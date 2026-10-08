# -*- coding: utf-8 -*-
"""数据质量中心：采集完整性、累计值回退与收入异常检测。

规则刻意保持确定性，便于业务人员追溯：
* 平台覆盖：配置中的平台在指定日期是否有至少一条已入库数据；
* 门店覆盖：优先以月度目标表中的门店为基准，否则与上一个有数据日对比；
* 累计回退：同月、同门店、同平台、同指标不应小于上一次采集值；
* 收入异常：单日收入相较此前最多 7 个采集日的均值偏离超过 2.5 个标准差。

该模块只读取 ``daily_summary`` 和 ``tasks``，不会修改业务数据。
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import mean, pstdev
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core.config import get as config_get
from core.collection_settings import enabled_platform_ids
from core.db import get_connection
from core.metric_rules import allows_decrease
from core.targets import load_store_targets
from crawlers.report_summary import (
    INCOME_COLUMNS,
    WHALE_CASH_INCLUDES_KPAY_VENUES,
    has_jingjian_cash,
    kpay_of_metrics,
)


DEFAULT_ANOMALY_THRESHOLD = 2.5
DEFAULT_ANOMALY_LOOKBACK = 7


def _platforms() -> Dict[str, str]:
    """返回已配置的平台标识与展示名。"""
    configured = config_get("platforms", {}) or {}
    if not isinstance(configured, dict):
        return {}
    catalog = [
        (str(platform), str((item or {}).get("name") or platform))
        for platform, item in configured.items()
        if isinstance(item, dict)
    ]
    enabled = enabled_platform_ids(catalog)
    return {platform: name for platform, name in catalog if platform in enabled}


def _parse_metrics(raw: Any) -> Dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _read_summary_rows(start: str, end: str) -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT date, venue, platform, metrics_json, updated_at "
            "FROM daily_summary WHERE date>=? AND date<=? "
            "ORDER BY date, venue, platform",
            (start, end),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _available_dates(end_date: str) -> List[str]:
    month_start = end_date[:8] + "01"
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT date FROM daily_summary WHERE date>=? AND date<=? "
            "ORDER BY date",
            (month_start, end_date),
        ).fetchall()
        return [str(row["date"]) for row in rows]
    finally:
        conn.close()


def _latest_tasks(date: str) -> Dict[str, Dict[str, Any]]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT platform, status, error_msg, created_at, finished_at FROM tasks t1 "
            "WHERE date=? AND created_at=("
            " SELECT MAX(created_at) FROM tasks t2 "
            " WHERE t2.platform=t1.platform AND t2.date=?"
            ")",
            (date, date),
        ).fetchall()
        return {str(row["platform"]): dict(row) for row in rows}
    finally:
        conn.close()


def _income_by_venue(rows: Iterable[Dict[str, Any]]) -> Dict[str, float]:
    """按报表收入字段计算每个门店的累计收入，并处理 KPay 重复统计。"""
    materialized = list(rows)
    kpay_totals: Dict[str, float] = defaultdict(float)
    for row in materialized:
        metrics = _parse_metrics(row.get("metrics_json"))
        venue = str(row.get("venue") or "")
        if venue in WHALE_CASH_INCLUDES_KPAY_VENUES:
            kpay_totals[venue] += kpay_of_metrics(metrics)

    result: Dict[str, float] = defaultdict(float)
    for row in materialized:
        venue = str(row.get("venue") or "")
        if not venue:
            continue
        metrics = _parse_metrics(row.get("metrics_json"))
        income = sum(
            value for key, raw in metrics.items()
            if key in INCOME_COLUMNS and (value := _number(raw)) is not None
        )
        if venue in WHALE_CASH_INCLUDES_KPAY_VENUES and has_jingjian_cash(metrics):
            income -= kpay_totals[venue]
        result[venue] += income
    return dict(result)


def _rows_by_date(rows: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["date"])].append(row)
    return dict(grouped)


def detect_cumulative_regressions(
    current_rows: Iterable[Dict[str, Any]], previous_rows: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """比较两个同月快照，返回累计指标下降项（仅比较共有的数值指标）。"""
    previous: Dict[Tuple[str, str], Dict[str, Any]] = {
        (str(row.get("venue") or ""), str(row.get("platform") or "")): _parse_metrics(row.get("metrics_json"))
        for row in previous_rows
    }
    issues: List[Dict[str, Any]] = []
    for row in current_rows:
        venue = str(row.get("venue") or "")
        platform = str(row.get("platform") or "")
        old_metrics = previous.get((venue, platform))
        if old_metrics is None:
            continue
        for metric, current_raw in _parse_metrics(row.get("metrics_json")).items():
            current = _number(current_raw)
            previous_value = _number(old_metrics.get(metric))
            if current is None or previous_value is None or current >= previous_value:
                continue
            if allows_decrease(platform, metric, current):
                continue
            issues.append({
                "type": "cumulative_regression",
                "severity": "warning",
                "platform": platform,
                "venue": venue,
                "metric": str(metric),
                "previous": round(previous_value, 2),
                "current": round(current, 2),
                "delta": round(current - previous_value, 2),
                "message": f"{venue} 的 {metric} 较上次采集下降 {previous_value - current:.2f}",
            })
    return issues


def detect_income_anomalies(
    income_snapshots: List[Tuple[str, Dict[str, float]]],
    threshold: float = DEFAULT_ANOMALY_THRESHOLD,
) -> List[Dict[str, Any]]:
    """由按日期排列的累计收入快照计算单日增量，并检测末日门店异常。"""
    if len(income_snapshots) < 5:
        return []

    daily_values: List[Tuple[str, Dict[str, float]]] = []
    previous: Dict[str, float] = {}
    previous_date = None
    segment: List[Tuple[str, Dict[str, float]]] = []
    for date, cumulative in income_snapshots:
        current_date = datetime.strptime(str(date), "%Y-%m-%d").date()
        if previous_date is None or current_date - previous_date != timedelta(days=1):
            if segment:
                daily_values.extend(segment)
            segment = []
        else:
            values = {
                venue: value - previous.get(venue, 0.0)
                for venue, value in cumulative.items()
            }
            segment.append((str(date), values))
        previous = cumulative
        previous_date = current_date

    if segment:
        daily_values.extend(segment)
    if not segment or not daily_values or daily_values[-1][0] != str(income_snapshots[-1][0]):
        # 目标快照没有自然日前一天时，不能把跨采集日差额解释成单日异常。
        return []
    target_date, target_values = segment[-1]
    history = segment[:-1][-DEFAULT_ANOMALY_LOOKBACK:]
    issues: List[Dict[str, Any]] = []
    for venue, value in target_values.items():
        samples = [daily.get(venue) for _, daily in history if venue in daily]
        if len(samples) < 3:
            continue
        avg = mean(samples)
        std = pstdev(samples)
        if std <= 0:
            continue
        z_score = (value - avg) / std
        if abs(z_score) < threshold:
            continue
        issues.append({
            "type": "income_anomaly",
            "severity": "warning",
            "venue": venue,
            "date": target_date,
            "daily_income": round(value, 2),
            "baseline": round(avg, 2),
            "z_score": round(z_score, 2),
            "direction": "up" if z_score > 0 else "down",
            "message": f"{venue} 单日收入较近 {len(samples)} 次采集{'偏高' if z_score > 0 else '偏低'}（{z_score:+.2f}σ）",
        })
    return sorted(issues, key=lambda item: abs(item["z_score"]), reverse=True)


def inspect(date: str, venue_scope=None) -> Dict[str, Any]:
    """生成指定日期的数据质量报告。"""
    parsed_date = datetime.strptime(date, "%Y-%m-%d").date()
    platforms = _platforms()
    dates = _available_dates(date)
    rows = _read_summary_rows(date[:8] + "01", date)
    if venue_scope is not None:
        scope = {str(value).strip() for value in venue_scope if str(value).strip()}
        rows = [row for row in rows if str(row.get("venue") or "").strip() in scope]
    by_date = _rows_by_date(rows)
    current_rows = by_date.get(date, [])
    current_platforms = {str(row["platform"]) for row in current_rows}
    task_map = _latest_tasks(date)

    # 月初第一天没有前一日快照；其余日期必须和自然日的前一天比较，
    # 不能跳过缺失日期去比较更早的快照，否则会掩盖数据连续性问题。
    previous_date = (
        None
        if parsed_date.day == 1
        else (parsed_date - timedelta(days=1)).isoformat()
    )

    platform_issues: List[Dict[str, Any]] = []
    platform_status: List[Dict[str, Any]] = []
    retry_statuses = {"failed", "stopped"}
    for platform, name in platforms.items():
        task = task_map.get(platform)
        has_data = platform in current_platforms
        status = str((task or {}).get("status") or ("success" if has_data else "missing"))
        item = {
            "platform": platform,
            "name": name,
            "has_data": has_data,
            "status": status,
            "rows": sum(1 for row in current_rows if row["platform"] == platform),
            "error": (task or {}).get("error_msg") or "",
        }
        platform_status.append(item)
        if has_data and status in retry_statuses:
            platform_issues.append({
                "type": "platform_failed",
                "severity": "critical",
                "platform": platform,
                "name": name,
                "status": status,
                "message": (
                    f"{name} 最新采集任务失败：{item['error']}；当前展示的可能是旧数据"
                    if status == "failed" and item["error"]
                    else f"{name} 最新采集任务{'失败' if status == 'failed' else '已停止'}；当前展示的可能是旧数据"
                ),
            })
        elif not has_data:
            platform_issues.append({
                "type": "platform_missing",
                "severity": "critical" if status in retry_statuses else "warning",
                "platform": platform,
                "name": name,
                "status": status,
                "message": (
                    f"{name} 最近任务失败：{item['error']}"
                    if status == "failed" and item["error"]
                    else f"{name} 在 {date} 没有入库数据"
                ),
            })

    current_venues = {str(row["venue"]) for row in current_rows if row.get("venue")}
    previous_rows = by_date.get(previous_date or "", [])
    expected_venues = set(load_store_targets(date[:7]))
    if venue_scope is not None:
        expected_venues &= {
            str(value).strip() for value in venue_scope if str(value).strip()
        }
    venue_basis = "目标表"
    if not expected_venues and previous_rows:
        expected_venues = {str(row["venue"]) for row in previous_rows if row.get("venue")}
        venue_basis = "上次采集"
    missing_venues = sorted(expected_venues - current_venues)
    venue_issues = [
        {
            "type": "venue_missing",
            "severity": "warning",
            "venue": venue,
            "message": f"门店 {venue} 未出现在当日汇总中（基准：{venue_basis}）",
        }
        for venue in missing_venues
    ]

    regressions = detect_cumulative_regressions(current_rows, previous_rows) if previous_rows else []
    continuity_issues: List[Dict[str, Any]] = []
    if previous_date and current_rows and not previous_rows:
        continuity_issues.append({
            "type": "previous_day_missing",
            "severity": "warning",
            "date": date,
            "previous_date": previous_date,
            "message": f"缺少前一日（{previous_date}）快照，无法完成累计值连续性比较",
        })
    snapshots = [
        (d, _income_by_venue(by_date[d]))
        for d in dates
        if d in by_date
    ]
    anomalies = detect_income_anomalies(snapshots)
    issues = platform_issues + venue_issues + regressions + continuity_issues + anomalies
    critical_count = sum(1 for issue in issues if issue["severity"] == "critical")
    warning_count = sum(1 for issue in issues if issue["severity"] == "warning")

    return {
        "date": date,
        "previous_date": previous_date or None,
        "data_rows": len(current_rows),
        "platforms": platform_status,
        "coverage": {
            "expected_platforms": len(platforms),
            "covered_platforms": len(current_platforms & set(platforms)),
            "expected_venues": len(expected_venues),
            "covered_venues": len(current_venues & expected_venues) if expected_venues else len(current_venues),
            "basis": venue_basis if expected_venues else "当日汇总",
        },
        "comparison": {
            "date": previous_date,
            "available": bool(previous_date and previous_rows),
            "skipped": parsed_date.day == 1,
        },
        "summary": {
            "critical": critical_count,
            "warning": warning_count,
            "healthy": len(issues) == 0,
            "regressions": len(regressions),
            "continuity_missing": len(continuity_issues),
            "anomalies": len(anomalies),
        },
        "missing_platforms": [item["platform"] for item in platform_status if not item["has_data"]],
        "retry_platforms": [
            item["platform"] for item in platform_status
            if not item["has_data"] or item["status"] in retry_statuses
        ],
        "issues": issues,
    }
