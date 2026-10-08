# -*- coding: utf-8 -*-
"""数据大屏后端数据（减法逻辑：累计差值=单日收入）"""

import json
import calendar
from datetime import datetime, timedelta
from typing import Optional, Iterable

from core.db import get_connection
from core.targets import load_daily_active_venues, load_store_regions, load_store_targets
from core.venue_lifecycle import operating_venues_on, list_lifecycle_records
from core.operating_brief import enrich_dashboard
from crawlers.report_summary import get_operating_venues, is_current_operating_venue

def _active_venue_set(fallback_values=(), target_date: Optional[str] = None):
    """按目标日期统一门店生命周期口径；未维护日期时沿用当前状态。"""
    operating = get_operating_venues()
    if operating is not None:
        current = {
            str(value).strip() for value in operating
            if is_current_operating_venue(value)
        }
    else:
        target_month = (target_date or datetime.now().strftime("%Y-%m-%d"))[:7]
        configured = set(load_store_targets(target_month)) | set(load_daily_active_venues())
        candidates = configured or set(fallback_values)
        current = {
            str(value).strip() for value in candidates
            if is_current_operating_venue(value)
        }
    candidates = current | {
        str(value).strip() for value in fallback_values if str(value).strip()
    }
    return operating_venues_on(
        target_date or datetime.now().strftime("%Y-%m-%d"),
        candidates,
        current,
    )


def _last_month_same_date(dt):
    """返回上个月同日；目标日超过上月天数时钳制到上月末（如 3/31 -> 2/28/29）"""
    prev_last = dt.replace(day=1) - timedelta(days=1)
    return prev_last.replace(day=min(dt.day, prev_last.day))


def _load_venue_income(date: str, venue_scope: Optional[Iterable[str]] = None) -> dict:
    """加载某日累计数据的场地→收入汇总"""
    conn = get_connection()
    try:
        scope = {str(value).strip() for value in (venue_scope or []) if str(value).strip()}
        if venue_scope is not None and not scope:
            return None
        query = "SELECT venue, metrics_json FROM daily_summary WHERE date=?"
        params = [date]
        if venue_scope is not None:
            query += " AND venue IN (" + ",".join("?" for _ in scope) + ")"
            params.extend(sorted(scope))
        rows = conn.execute(query, tuple(params)).fetchall()
        if not rows:
            return None  # 无数据

        data_list = []
        for r in rows:
            item = json.loads(r["metrics_json"])
            if isinstance(item, dict):
                item["场地"] = r["venue"]
                data_list.append(item)

        import importlib
        mod = importlib.import_module("crawlers.report_summary")
        # 历史门店可能已不在当前组织清单中，仍需按快照里的真实门店补回报表行。
        snapshot_venues = {
            str(row["venue"]).strip() for row in rows if str(row["venue"]).strip()
        }
        result = mod.main(data_list, active_venues=snapshot_venues)
        if not result or len(result) < 2:
            return None

        income_idx = result[0].index("收入汇总") if "收入汇总" in result[0] else None
        venue_idx = result[0].index("场地") if "场地" in result[0] else 0
        if income_idx is None:
            return None

        return {row[venue_idx]: row[income_idx] for row in result[1:] if row[venue_idx] != "合计"}
    finally:
        conn.close()


def get_dashboard_status(target_date: str, venue_scope: Optional[Iterable[str]] = None) -> dict:
    """检查大屏所需数据是否齐全"""
    if venue_scope is not None:
        venue_scope = {str(value).strip() for value in venue_scope if str(value).strip()}
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    prev_day = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
    prev_day2 = (dt - timedelta(days=2)).strftime("%Y-%m-%d")
    last_month_same = _last_month_same_date(dt).strftime("%Y-%m-%d")

    dates_check = [
        (target_date, f"{dt.month}/{dt.day}累计"),
        (prev_day, f"{int(prev_day[5:7])}/{int(prev_day[-2:])}累计"),
    ]
    # 每月 2 日的昨日收入就是 1 日累计，不需要上月末作基线。
    if dt.day != 2:
        dates_check.append((prev_day2, f"{int(prev_day2[5:7])}/{int(prev_day2[-2:])}累计"))
    dates_check.append((last_month_same, "上月同日累计"))

    missing = []
    available = []
    for d, label in dates_check:
        data = (
            _load_venue_income(d)
            if venue_scope is None
            else _load_venue_income(d, venue_scope)
        )
        if data is None:
            missing.append({"date": d, "label": label})
        else:
            available.append({"date": d, "label": label, "total": sum(data.values())})

    return {
        "target": target_date,
        "ready": len(missing) == 0,
        "dates_available": available,
        "dates_missing": missing,
        "data_issues": _snapshot_issues([day for day, _ in dates_check], venue_scope),
        "coverage_note": "按当月已出现的门店和平台检查；未曾入库的数据源仍需核对。",
    }


def _snapshot_issues(dates, venue_scope=None):
    """检查同月已知门店/平台缺口和累计区间，不用当前在营名单裁剪历史。"""
    scope = None if venue_scope is None else set(venue_scope)
    lifecycle = list_lifecycle_records()
    issues = []
    conn = get_connection()
    try:
        for month in sorted({day[:7] for day in dates}):
            checked = sorted(day for day in dates if day.startswith(month))
            rows = conn.execute(
                "SELECT date,venue,platform,period_start FROM daily_summary "
                "WHERE date>=? AND date<=? ORDER BY date",
                (month + "-01", checked[-1]),
            ).fetchall()
            snapshots = {}
            first_seen = {}
            for row in rows:
                if scope is not None and row["venue"] not in scope:
                    continue
                pair = (row["venue"], row["platform"])
                first_seen.setdefault(pair, row["date"])
                snapshots.setdefault(row["date"], {})[pair] = row["period_start"]
            for day in checked:
                snapshot = snapshots.get(day, {})
                if not snapshot:
                    continue  # 整日缺失由 dates_missing 表达。
                for pair, first in sorted(first_seen.items()):
                    closed = lifecycle.get(pair[0], {}).get("closed_on")
                    if closed and closed < month + "-01":
                        continue
                    if first <= day and pair not in snapshot:
                        issues.append({"date": day, "venue": pair[0], "platform": pair[1],
                                       "reason": "venue_platform_missing", "blocking": False})
                for pair, start in sorted(snapshot.items()):
                    if start != month + "-01":
                        issues.append({"date": day, "venue": pair[0], "platform": pair[1],
                                       "reason": "range_mismatch" if start else "range_unknown",
                                       "blocking": bool(start)})
    finally:
        conn.close()
    return issues


def get_dashboard_data(target_date: str, venue_scope: Optional[Iterable[str]] = None) -> dict:
    """计算大屏完整数据（减法逻辑）"""
    if venue_scope is not None:
        venue_scope = {str(value).strip() for value in venue_scope if str(value).strip()}
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    prev_day = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
    prev_day2 = (dt - timedelta(days=2)).strftime("%Y-%m-%d")
    last_month_same = _last_month_same_date(dt).strftime("%Y-%m-%d")

    # 加载计算所需的累计快照；每月 2 日无需上月末基线。
    def load_income(day: str):
        return (
            _load_venue_income(day)
            if venue_scope is None
            else _load_venue_income(day, venue_scope)
        )

    curr = load_income(target_date)
    prev = load_income(prev_day)
    prev2 = load_income(prev_day2) if dt.day != 2 else None
    last_month = load_income(last_month_same)

    if curr is None or prev is None:
        return {"ready": False, "error": "数据不完整"}

    active_venues = _active_venue_set(
        set(curr) | set(prev),
        target_date=target_date,
    )
    if venue_scope is not None:
        active_venues &= set(venue_scope)

    # 月累计在每月 1 日重置，不能减去上月末累计。
    all_venues = set(curr) | set(prev) | set(prev2 or {})
    today_daily = {}   # 目标日单日；月初直接取 curr
    yesterday_daily = {}  # 前日单日；每月 2 日直接取 prev
    curr_month_cumulative = curr  # 本月累计
    last_month_cumulative = last_month or {}

    for v in all_venues:
        c = curr.get(v, 0)
        p = prev.get(v, 0)
        today_daily[v] = c if dt.day == 1 else c - p
        if dt.day == 2:
            yesterday_daily[v] = p
        elif prev2:
            p2 = prev2.get(v, 0)
            yesterday_daily[v] = p - p2

    # 核心指标
    today_total = sum(today_daily.values())
    yesterday_total = sum(yesterday_daily.values())
    curr_month_total = sum(curr_month_cumulative.values())
    last_month_total = sum(last_month_cumulative.values())

    day_change = today_total - yesterday_total
    day_change_pct = (day_change / yesterday_total * 100) if yesterday_total else None

    month_change = curr_month_total - last_month_total
    month_change_pct = (month_change / last_month_total * 100) if last_month_total else None

    # 门店目标（月度目标表「最终核定目标」）
    store_targets = {
        venue: target
        for venue, target in load_store_targets(target_date[:7]).items()
        if venue in active_venues and target > 0
    }          # {场地: 目标(元)}，只保留在营门店
    monthly_target_wan = (
        sum(store_targets.values()) / 10000
        if store_targets
        else None
    )

    # 日均收入
    days_passed = dt.day
    avg_daily = curr_month_total / days_passed if days_passed > 0 else 0
    target_actual = sum(curr.get(venue, 0) for venue in store_targets)
    completion_rate = target_actual / (monthly_target_wan * 10000) * 100 if monthly_target_wan else None
    days_in_month = calendar.monthrange(dt.year, dt.month)[1]

    # 门店完成度 = 本月累计 / 门店目标
    store_completion = []
    completion_venues = active_venues
    for venue in sorted(completion_venues):
        target = float(store_targets.get(venue, 0) or 0)
        actual = curr_month_cumulative.get(venue, 0) or 0
        pct = actual / target * 100 if target else 0
        store_completion.append({
            "venue": venue,
            "target": round(target, 2),
            "actual": round(actual, 2),
            "pct": round(pct, 2),
            "target_available": bool(target),
        })
    store_completion.sort(key=lambda x: x["pct"])

    # 区域完成度 = 区域内门店实际/目标之和（需门店→区域映射）
    region_completion = []
    store_regions = load_store_regions()
    if store_regions:
        groups = {}
        for item in store_completion:
            if not item["target_available"]:
                continue
            region = store_regions.get(item["venue"])
            if not region:
                continue
            g = groups.setdefault(region, {"target": 0.0, "actual": 0.0})
            g["target"] += item["target"]
            g["actual"] += item["actual"]
        region_completion = [
            {
                "region": r,
                "target": round(g["target"], 2),
                "actual": round(g["actual"], 2),
                "pct": round(g["actual"] / g["target"] * 100, 2) if g["target"] else 0,
            }
            for r, g in groups.items()
        ]
        region_completion.sort(key=lambda x: x["pct"])

    # 内地/香港分拆
    month_hk = sum(v for k, v in curr_month_cumulative.items() if "香港" in str(k))
    month_cn = curr_month_total - month_hk
    today_hk = sum(v for k, v in today_daily.items() if "香港" in str(k))
    today_cn = today_total - today_hk

    # 排行榜
    def rank(data, is_hk=None):
        filtered = {k: v for k, v in data.items() if k in active_venues}
        if is_hk is True:
            filtered = {k: v for k, v in filtered.items() if "香港" in str(k)}
        elif is_hk is False:
            filtered = {k: v for k, v in filtered.items() if "香港" not in str(k)}
        return sorted(filtered.items(), key=lambda x: -x[1])[:3]

    # 区域
    region_map = {"香港": "香港", "东莞": "东莞", "深圳": "深圳", "广州": "广州", "江门": "江门", "福州": "福建", "福建": "福建"}
    regions = {}
    for venue, val in curr_month_cumulative.items():
        r = store_regions.get(venue)
        if not r:
            r = next((rn for kw, rn in region_map.items() if kw in str(venue)), "其他")
        regions[r] = regions.get(r, 0) + val

    # 每日趋势
    trend_start = dt.replace(day=1).strftime("%Y-%m-%d")
    check_dates = {(dt.replace(day=1) + timedelta(days=n)).strftime("%Y-%m-%d") for n in range(dt.day)}
    check_dates.update([prev_day, last_month_same])
    if dt.day != 2:
        check_dates.add(prev_day2)
    issues = _snapshot_issues(check_dates, venue_scope)
    invalid_dates = {item["date"] for item in issues if item["blocking"]}
    daily_trend = _load_daily_trend(trend_start, target_date, active_venues=venue_scope,
                                   invalid_dates=invalid_dates)

    result = {
        "ready": True,
        "range": f"{dt.year}-{dt.month:02d}-01 ~ {target_date}",
        "target_date": target_date,
        "days_passed": days_passed,
        "today_total": today_total,
        "today_cn": today_cn,
        "today_hk": today_hk,
        "yesterday_total": yesterday_total,
        "day_change": day_change,
        "day_change_pct": round(day_change_pct, 2) if day_change_pct is not None else None,
        "curr_month_total": curr_month_total,
        "month_hk": month_hk,
        "month_cn": month_cn,
        "avg_daily": avg_daily,
        "completion_rate": round(completion_rate, 2) if completion_rate is not None else None,
        "month_target": round(monthly_target_wan, 2) if monthly_target_wan is not None else None,
        "target_daily": monthly_target_wan / days_in_month if monthly_target_wan else None,
        "target_month": target_date[:7],
        "days_in_month": days_in_month,
        "target_actual": round(target_actual, 2),
        "target_store_count": len(store_targets),
        "missing_target_venues": sorted(active_venues - set(store_targets)),
        "store_targets_loaded": bool(store_targets),
        "store_completion": store_completion,
        "region_completion": region_completion,
        "last_month_total": last_month_total,
        "month_change": month_change,
        "month_change_pct": round(month_change_pct, 2) if month_change_pct is not None else None,
        "top_cn": rank(curr_month_cumulative, is_hk=False),
        "top_hk": rank(curr_month_cumulative, is_hk=True),
        "top_today_gain": rank(today_daily),
        "regions": regions,
        "daily_trend": daily_trend,
        "data_issues": issues,
        "income_scope": "全部历史门店（权限范围内）",
        "coverage_note": "按当月已出现的门店和平台检查；未曾入库的数据源仍需核对。",
    }
    # 已知缺口不伪装成零或完整收入；不受影响的指标仍可查看。
    if target_date in invalid_dates:
        for key in ("curr_month_total", "month_hk", "month_cn", "avg_daily", "completion_rate", "target_actual"):
            result[key] = None
        result.update(top_cn=[], top_hk=[], regions={}, region_completion=[])
        for item in result["store_completion"]:
            item.update(actual=None, pct=None)
    if target_date in invalid_dates or (dt.day != 1 and prev_day in invalid_dates):
        for key in ("today_total", "today_cn", "today_hk"):
            result[key] = None
        result["top_today_gain"] = []
    if prev_day in invalid_dates or (dt.day != 2 and (prev2 is None or prev_day2 in invalid_dates)):
        result["yesterday_total"] = None
    if result["today_total"] is None or result["yesterday_total"] is None:
        result.update(day_change=None, day_change_pct=None)
    if last_month is None or last_month_same in invalid_dates:
        result["last_month_total"] = None
    if result["curr_month_total"] is None or result["last_month_total"] is None:
        result.update(month_change=None, month_change_pct=None)
    return enrich_dashboard(result, target_date, venue_scope)


def _load_daily_trend(start: str, end: str, active_venues=None, invalid_dates=()) -> list:
    """月初累计即当日收入；其他日期只减自然前一天，缺快照时保留断点。"""
    def visible_values(values):
        if values is None or active_venues is None:
            return values
        return {
            venue: value for venue, value in values.items()
            if venue in active_venues
        }

    day = datetime.strptime(start, "%Y-%m-%d")
    end_day = datetime.strptime(end, "%Y-%m-%d")
    previous = None
    if day.day != 1:
        previous_date = (day - timedelta(days=1)).strftime("%Y-%m-%d")
        previous = visible_values(
            _load_venue_income(previous_date)
        )
        if previous_date in invalid_dates:
            previous = None
    prev_total = sum(previous.values()) if previous is not None else None
    trend = []
    while day <= end_day:
        date = day.strftime("%Y-%m-%d")
        data = visible_values(_load_venue_income(date))
        if date in invalid_dates:
            data = None
        total = sum(data.values()) if data is not None else None
        daily = None
        if total is not None:
            if day.day == 1:
                daily = total
            elif prev_total is not None:
                daily = total - prev_total
        trend.append({"date": date, "daily": daily, "cumulative": total})
        prev_total = total
        day += timedelta(days=1)
    return trend
