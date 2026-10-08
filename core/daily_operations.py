# -*- coding: utf-8 -*-
"""每日经营数据：从月累计快照计算单日收入。

该模块只读取 daily_summary，不写入业务数据。旧快照没有采集范围证明时，
会保留已知差值但将状态标记为 ``unverified``，避免把不同口径的累计值静默相减。
"""

import json
from datetime import date as date_type, datetime, timedelta
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from core.config import get as config_get
from core.db import get_connection
from core.metric_rules import allows_decrease
from core.targets import load_daily_active_venues, load_store_regions, load_store_targets
from core.venue_lifecycle import operating_venues_on
from crawlers.report_summary import (
    WHALE_CASH_INCLUDES_KPAY_VENUES,
    get_operating_venues,
    is_current_operating_venue,
)


PLATFORM_FIELDS: Dict[str, Tuple[str, ...]] = {
    "meituan": ("美团收款",),
    "yuntai": ("芸苔非团购", "芸苔远程取币"),
    "youcaihua": ("油菜花现金", "油菜花微信", "油菜花支付宝", "油菜花盈客宝"),
    "douyin": ("抖音收款",),
    "leyaoyao": ("乐摇摇非现金", "乐摇摇现金"),
    "duojinbao": ("多金宝现金", "多金宝非现金"),
    "jingjian": ("鲸舰非现金", "鲸舰现金"),
    "starthing": ("StarThing非现金", "StarThing现金"),
    "huilian": ("汇联现金", "汇联非现金"),
    "kpay": ("Kpay收款",),
    "octopus": ("八达通收款",),
    "coin_exchange": ("兑币机收款",),
}

# KPay 当前网络不稳定，但不应阻断其他平台的经营数据展示。
# 采集任务失败仍会保留在 optional_source_warnings 中，待网络恢复后再处理。
NON_BLOCKING_PLATFORMS = frozenset({"kpay"})


def _parse_date(value: str) -> date_type:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise ValueError("日期格式错误，请使用 YYYY-MM-DD") from exc


def _number(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _metrics(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _configured_platforms() -> Dict[str, str]:
    configured = config_get("platforms", {}) or {}
    if not isinstance(configured, Mapping):
        return {}
    return {
        str(platform): str(item.get("name") or platform)
        for platform, item in configured.items()
        if isinstance(item, Mapping)
    }


def _active_venues(fallback_values=(), target_date: Optional[str] = None) -> set[str]:
    """按目标日期判定营业门店；未维护日期时沿用当前负责人状态。"""
    fallback = {
        str(venue).strip() for venue in fallback_values if str(venue).strip()
    }
    operating_venues = get_operating_venues()
    if operating_venues is not None:
        current = {
            venue for venue in operating_venues
            if is_current_operating_venue(venue)
        }
    else:
        target_month = (target_date or date_type.today().isoformat())[:7]
        target_venues = {
            str(venue).strip()
            for venue in load_store_targets(target_month)
            if str(venue).strip()
        }
        configured = target_venues | load_daily_active_venues()
        current = {
            str(venue).strip()
            for venue in (configured or fallback)
            if is_current_operating_venue(venue)
        }
    candidates = current | fallback
    return operating_venues_on(
        target_date or date_type.today().isoformat(),
        candidates,
        current,
    )


def _load_rows(conn, target_date: str) -> Dict[Tuple[str, str], Dict[str, Any]]:
    rows = conn.execute(
        "SELECT date, venue, platform, metrics_json, period_start, source_task_id, updated_at "
        "FROM daily_summary WHERE date=?",
        (target_date,),
    ).fetchall()
    result: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        result[(str(row["venue"]), str(row["platform"]))] = {
            "date": str(row["date"]),
            "venue": str(row["venue"]),
            "platform": str(row["platform"]),
            "metrics": _metrics(row["metrics_json"]),
            "period_start": row["period_start"],
            "source_task_id": row["source_task_id"],
            "updated_at": row["updated_at"],
        }
    return result


def _load_historical_pairs(conn, month_start: str, target_date: str) -> set[Tuple[str, str]]:
    rows = conn.execute(
        "SELECT DISTINCT venue, platform FROM daily_summary "
        "WHERE date>=? AND date<=?",
        (month_start, target_date),
    ).fetchall()
    return {(str(row["venue"]), str(row["platform"])) for row in rows}


def _latest_task_status(conn, target_date: str) -> Dict[str, str]:
    rows = conn.execute(
        "SELECT platform, status, created_at FROM tasks WHERE date=? "
        "ORDER BY created_at DESC",
        (target_date,),
    ).fetchall()
    result: Dict[str, str] = {}
    for row in rows:
        result.setdefault(str(row["platform"]), str(row["status"] or ""))
    return result


def _raw_income(row: Optional[Mapping[str, Any]]) -> Optional[float]:
    if not row:
        return None
    fields = PLATFORM_FIELDS.get(str(row.get("platform")), ())
    values = [_number(row.get("metrics", {}).get(field)) for field in fields]
    return round(sum(value for value in values if value is not None), 2)


def _corrected_income(
    snapshot: Mapping[Tuple[str, str], Mapping[str, Any]],
    pair: Tuple[str, str],
) -> Optional[float]:
    row = snapshot.get(pair)
    raw = _raw_income(row)
    if raw is None:
        return None
    venue, platform = pair
    if platform == "jingjian" and venue in WHALE_CASH_INCLUDES_KPAY_VENUES:
        kpay = _raw_income(snapshot.get((venue, "kpay")))
        if kpay is not None:
            raw -= kpay
    return round(raw, 2)


def _regression_warnings(
    current: Mapping[Tuple[str, str], Mapping[str, Any]],
    previous: Mapping[Tuple[str, str], Mapping[str, Any]],
) -> list[Dict[str, Any]]:
    warnings: list[Dict[str, Any]] = []
    for pair in sorted(set(current) & set(previous)):
        current_metrics = current[pair].get("metrics", {})
        previous_metrics = previous[pair].get("metrics", {})
        for metric, current_raw in current_metrics.items():
            current_value = _number(current_raw)
            previous_value = _number(previous_metrics.get(metric))
            if (
                current_value is not None
                and previous_value is not None
                and current_value < previous_value
                and not allows_decrease(pair[1], metric, current_value)
            ):
                warnings.append({
                    "type": "cumulative_regression",
                    "venue": pair[0],
                    "platform": pair[1],
                    "metric": str(metric),
                    "previous": round(previous_value, 2),
                    "current": round(current_value, 2),
                })
    return warnings


def _round_or_none(value: Optional[float]) -> Optional[float]:
    return round(value, 2) if value is not None else None


def _get_daily_operations(
    target_date: str,
    venue: str = "",
    venue_scope: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """返回指定自然日的收入差值、平台拆分和数据完整性状态。"""
    target = _parse_date(target_date)
    target_text = target.isoformat()
    month_start = target.replace(day=1).isoformat()
    first_day = target.day == 1
    previous_text = None if first_day else (target - timedelta(days=1)).isoformat()
    required_dates = [target_text] if first_day else [previous_text, target_text]

    conn = get_connection()
    try:
        snapshots = {target_text: _load_rows(conn, target_text)}
        if previous_text:
            snapshots[previous_text] = _load_rows(conn, previous_text)
        historical_pairs = _load_historical_pairs(conn, month_start, target_text)
        task_statuses = {
            current_date: _latest_task_status(conn, current_date)
            for current_date in required_dates
        }
    finally:
        conn.close()

    active_venues = _active_venues(
        (pair[0] for pair in historical_pairs),
        target_date=target_text,
    )
    if venue_scope is not None:
        active_venues &= {
            str(value).strip() for value in venue_scope if str(value).strip()
        }
    snapshots = {
        snapshot_date: {
            pair: row
            for pair, row in snapshot.items()
            if pair[0] in active_venues
        }
        for snapshot_date, snapshot in snapshots.items()
    }
    historical_pairs = {
        pair for pair in historical_pairs if pair[0] in active_venues
    }

    current = snapshots[target_text]
    previous = snapshots.get(previous_text, {}) if previous_text else {}
    configured = _configured_platforms()
    available_venues = sorted({pair[0] for pair in historical_pairs})
    selected_venue = str(venue or "").strip() or None
    if selected_venue and selected_venue not in active_venues:
        selected_venue = None

    missing_required_dates = [
        {"date": current_date, "reason": "snapshot_missing"}
        for current_date in required_dates
        if not snapshots.get(current_date)
    ]
    range_issues = []
    for current_date in required_dates:
        expected_start = month_start
        rows = snapshots.get(current_date, {})
        if rows and any(row.get("period_start") != expected_start for row in rows.values()):
            range_issues.append({
                "date": current_date,
                "reason": "range_unknown" if any(
                    not row.get("period_start") for row in rows.values()
                ) else "range_mismatch",
            })

    missing_sources = []
    optional_source_warnings = []
    for current_date in required_dates:
        available_sources = {pair[1] for pair in snapshots.get(current_date, {})}
        for platform in configured:
            if platform not in available_sources:
                item = {
                    "date": current_date,
                    "platform": platform,
                    "name": configured[platform],
                    "reason": "platform_missing",
                }
                (optional_source_warnings if platform in NON_BLOCKING_PLATFORMS else missing_sources).append(item)
        for platform, status in task_statuses[current_date].items():
            if status in {"failed", "stopped"}:
                item = {
                    "date": current_date,
                    "platform": platform,
                    "name": configured.get(platform, platform),
                    "reason": "task_%s" % status,
                }
                (optional_source_warnings if platform in NON_BLOCKING_PLATFORMS else missing_sources).append(item)

    def _dedupe_source_warnings(items):
        deduped = {}
        for item in items:
            key = (item.get("date"), item.get("platform"))
            previous_item = deduped.get(key)
            if previous_item is None or previous_item.get("reason") == "platform_missing":
                deduped[key] = item
        return list(deduped.values())

    missing_sources = _dedupe_source_warnings(missing_sources)
    optional_source_warnings = _dedupe_source_warnings(optional_source_warnings)

    pair_scope = set(current) | set(previous) | historical_pairs
    venue_platform_missing = []
    if first_day and snapshots[target_text]:
        for pair in sorted(pair_scope):
            if pair in historical_pairs and pair not in current:
                venue_platform_missing.append({
                    "date": target_text,
                    "venue": pair[0],
                    "platform": pair[1],
                    "name": configured.get(pair[1], pair[1]),
                    "reason": "venue_platform_missing",
                })
    elif not first_day and snapshots[target_text] and snapshots[previous_text]:
        for pair in sorted(pair_scope | historical_pairs):
            if pair not in historical_pairs or pair[1] in NON_BLOCKING_PLATFORMS:
                continue
            current_has = pair in current
            previous_has = pair in previous
            if current_has != previous_has or not current_has:
                missing_date = previous_text if current_has else target_text
                venue_platform_missing.append({
                    "date": missing_date,
                    "venue": pair[0],
                    "platform": pair[1],
                    "name": configured.get(pair[1], pair[1]),
                    "reason": "venue_platform_missing",
                })
                if not current_has and not previous_has:
                    venue_platform_missing.append({
                        "date": previous_text,
                        "venue": pair[0],
                        "platform": pair[1],
                        "name": configured.get(pair[1], pair[1]),
                        "reason": "venue_platform_missing",
                    })

    value_pairs = set(current) if first_day else (set(current) | set(previous))
    platform_rows = []
    venue_values: Dict[str, Dict[str, Any]] = {}
    known_income = 0.0
    comparable_pairs = 0
    for pair in sorted(value_pairs):
        pair_venue, pair_platform = pair
        if selected_venue and pair_venue != selected_venue:
            continue
        current_income = _corrected_income(current, pair)
        previous_income = _corrected_income(previous, pair) if not first_day else 0.0
        complete_pair = current_income is not None and (first_day or previous_income is not None)
        if pair_platform in PLATFORM_FIELDS:
            complete_pair = complete_pair and all(
                snapshot.get(pair, {}).get("period_start") in (None, month_start)
                for snapshot in ([current] if first_day else [current, previous])
            )
        value = round(current_income - previous_income, 2) if complete_pair else None
        if complete_pair and pair_platform in PLATFORM_FIELDS:
            comparable_pairs += 1
        if complete_pair and value is not None:
            known_income += value
        entry = venue_values.setdefault(pair_venue, {"total_income": 0.0, "complete": True, "platforms": []})
        entry["platforms"].append({
            "platform": pair_platform,
            "name": configured.get(pair_platform, pair_platform),
            "applicable": True,
            "income": value,
            "status": "complete" if complete_pair else "missing",
        })
        if not complete_pair:
            entry["complete"] = False
        elif value is not None:
            entry["total_income"] += value

    platforms_by_id: Dict[str, Dict[str, Any]] = {}
    for item in venue_values.values():
        for platform in item["platforms"]:
            result = platforms_by_id.setdefault(platform["platform"], {
                "platform": platform["platform"],
                "name": platform["name"],
                "income": 0.0,
                "complete": True,
                "applicable": True,
            })
            if platform["income"] is None:
                result["complete"] = False
            else:
                result["income"] += platform["income"]

    for item in venue_values.values():
        item["total_income"] = _round_or_none(item["total_income"] if item["complete"] else None)
        item["platforms"] = sorted(item["platforms"], key=lambda value: value["name"])
        item.pop("complete", None)
    platforms = []
    for item in sorted(platforms_by_id.values(), key=lambda value: value["name"]):
        item["income"] = _round_or_none(item["income"] if item["complete"] else None)
        item["status"] = "complete" if item["complete"] else "missing"
        item.pop("complete", None)
        platforms.append(item)

    regressions = [] if first_day else _regression_warnings(current, previous)
    warnings = []
    warnings.extend(range_issues)
    warnings.extend(venue_platform_missing)
    warnings.extend(regressions)
    warnings.extend({**item, "reason": "optional_" + str(item.get("reason", "source"))}
                     for item in optional_source_warnings)
    if missing_required_dates:
        status = "missing"
    elif missing_sources or venue_platform_missing:
        status = "partial"
    elif range_issues:
        status = "unverified"
    else:
        status = "complete"

    updated_values = [
        row.get("updated_at")
        for snapshot in snapshots.values()
        for row in snapshot.values()
        if row.get("updated_at")
    ]
    store_regions = load_store_regions()
    venues = []
    for item_venue, item in sorted(venue_values.items()):
        venues.append({
            "venue": item_venue,
            "region": store_regions.get(item_venue) or (
                "香港" if "香港" in item_venue else "内地"
            ),
            "total_income": item["total_income"],
            "platforms": [
                platform for platform in item["platforms"]
                if platform["income"] is not None
            ],
        })

    return {
        "date": target_text,
        "previous_date": previous_text,
        "period_start": month_start,
        "formula": "month_start_baseline" if first_day else "current_minus_previous",
        "status": status,
        "selected_venue": selected_venue,
        "available_venues": available_venues,
        "total_income": _round_or_none(known_income) if status == "complete" else None,
        "known_income": _round_or_none(known_income) if comparable_pairs else None,
        "platforms": platforms,
        "venues": venues if not selected_venue else [],
        "missing_required_dates": missing_required_dates,
        "missing_sources": missing_sources,
        "optional_source_warnings": optional_source_warnings,
        "warnings": warnings,
        "updated_at": max(updated_values) if updated_values else None,
    }


def compare_daily(current, baseline):
    """未四舍五入的元金额比较；缺数据或非正基数不生成增长百分比。"""
    value, previous = current.get("known_income"), baseline.get("known_income")
    difference = round(value - previous, 2) if value is not None and previous is not None else None
    return {
        "date": baseline["date"],
        "income": previous,
        "change": difference,
        "change_pct": round(difference / previous * 100, 2) if difference is not None and previous > 0 else None,
        "complete": current["status"] == baseline["status"] == "complete",
    }


def get_daily_operations(target_date, venue="", venue_scope=None, include_comparisons=True):
    scope = set(venue_scope) if venue_scope is not None else None
    result = _get_daily_operations(target_date, venue, scope)
    if not include_comparisons:
        return result
    target = _parse_date(target_date)
    for key, days in (("previous_day", 1), ("previous_week", 7)):
        baseline = _get_daily_operations((target - timedelta(days=days)).isoformat(), venue, scope)
        if venue and baseline["selected_venue"] != venue:
            baseline.update(known_income=None, total_income=None, status="missing", venues=[])
        result[key] = compare_daily(result, baseline)
        if key == "previous_week":
            by_venue = {item["venue"]: item for item in baseline["venues"]}
            for item in result["venues"]:
                previous = by_venue.get(item["venue"], {}).get("total_income")
                value = item["total_income"]
                item["week_change"] = round(value - previous, 2) if value is not None and previous is not None else None
                item["week_change_pct"] = round((value - previous) / previous * 100, 2) if value is not None and previous is not None and previous > 0 else None
    return result
