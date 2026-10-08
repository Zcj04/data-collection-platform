"""老板日报所需指标，沿用权限范围及单日经营的累计差值口径。"""
import json
from datetime import datetime

from core.db import get_connection
from core.daily_operations import get_daily_operations
from crawlers import report_summary


# 用户日报中明确关注的两家新店，不根据首次采集日推断开业日。
FOCUS_VENUES = ("广州A店", "深圳龙华深圳N店Demo Finds")


def _load_metrics(target_date, scope):
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT venue, platform, metrics_json, period_start FROM daily_summary WHERE date=?",
            (target_date,),
        ).fetchall()
    finally:
        conn.close()
    rows = [row for row in rows if row["venue"] in scope]
    data = [dict(json.loads(row["metrics_json"]), 场地=row["venue"]) for row in rows]
    result = report_summary.main(data, active_venues=scope) if data else []
    metrics = {item["场地"]: item for item in (dict(zip(result[0], row)) for row in result[1:]) if item["场地"] != "合计"} if result else {}
    return metrics, rows


def enrich_dashboard(result, target_date, venue_scope=None):
    daily = get_daily_operations(target_date, venue_scope=venue_scope)
    result["daily_operations"] = daily
    result["today_total"] = daily["total_income"]
    result["today_known_income"] = daily["known_income"]
    result["yesterday_total"] = daily["previous_day"]["income"] if daily["previous_day"]["complete"] else None
    result["day_change"] = daily["previous_day"]["change"] if daily["previous_day"]["complete"] else None
    result["day_change_pct"] = daily["previous_day"]["change_pct"] if daily["previous_day"]["complete"] else None
    for key, is_hk in (("today_cn", False), ("today_hk", True)):
        values = [v["total_income"] for v in daily["venues"] if ("香港" in v["venue"]) == is_hk]
        result[key] = round(sum(values), 2) if daily["status"] == "complete" and all(v is not None for v in values) else None
    result["top_today_gain"] = sorted(
        [(v["venue"], v["total_income"]) for v in daily["venues"] if v["total_income"] is not None],
        key=lambda item: -item[1],
    )[:3]
    for point in result["daily_trend"]:
        day = daily if point["date"] == target_date else get_daily_operations(point["date"], venue_scope=venue_scope, include_comparisons=False)
        point.update(daily=day["known_income"], status=day["status"])

    target = result["month_target"]
    actual = result["target_actual"]
    remaining_days = result["days_in_month"] - result["days_passed"]
    gap = max(0, target * 10000 - actual) if target is not None and actual is not None else None
    result["target_progress"] = {
        "time_pct": round(result["days_passed"] / result["days_in_month"] * 100, 2),
        "gap": round(gap, 2) if gap is not None else None,
        "remaining_days": remaining_days,
        "needed_daily": round(gap / remaining_days, 2) if gap is not None and remaining_days > 0 else None,
    }
    scope = {item["venue"] for item in result["store_completion"]}
    metrics, rows = _load_metrics(target_date, scope)
    month_start = target_date[:7] + "-01"
    invalid = {row["venue"] for row in rows if row["period_start"] != month_start}
    payment_venues = {row["venue"] for row in rows if row["platform"] == "payment" and row["period_start"] == month_start}
    target_stores = {v["venue"] for v in result["store_completion"] if v["target_available"]}
    # 复用已有区域归属，不把缺货款记录解释为零货款。
    from core.targets import load_store_regions
    regions = load_store_regions()
    for region in result["region_completion"]:
        names = {v for v in target_stores if regions.get(v) == region["region"]}
        available = bool(names) and names <= payment_venues and not names & invalid
        payment = sum(float(metrics.get(v, {}).get("总货款", 0) or 0) for v in names) if available else None
        region["payment"] = round(payment, 2) if payment is not None else None
        region["payment_pct"] = round(payment / region["actual"] * 100, 2) if payment is not None and region["actual"] > 0 else None
        region["payment_missing"] = sorted(names - payment_venues)
    income = sum(float(metrics.get(v, {}).get("收入汇总", 0) or 0) for v in target_stores)
    payment = sum(float(metrics.get(v, {}).get("总货款", 0) or 0) for v in target_stores)
    payment_ready = bool(target_stores) and target_stores <= payment_venues and not target_stores & invalid
    coin = sum(float(v.get("投币合计", 0) or 0) for v in metrics.values())
    goods = sum(float(v.get("出货合计", 0) or 0) for v in metrics.values())
    result["operating_ratios"] = {
        "payment_pct": round(payment / income * 100, 2) if payment_ready and income > 0 else None,
        "coin_out_ratio": round(coin / goods, 2) if goods > 0 and not invalid else None,
        "coin": coin, "goods": goods,
        "note": "货款比仅计正目标门店；投币/出货按在营门店已入库数据，未设正常区间。缺少货款记录时比例留空。",
    }
    by_venue = {v["venue"]: v for v in daily["venues"]}
    result["focus_stores"] = [
        {"venue": venue, "monthly_income": metrics.get(venue, {}).get("收入汇总") if venue not in invalid else None,
         "daily_income": by_venue.get(venue, {}).get("total_income"),
         "completion": next((v["pct"] for v in result["store_completion"] if v["venue"] == venue and v["target_available"]), None)}
        for venue in FOCUS_VENUES if venue in scope
    ]
    result["brief_text"] = build_brief(result)
    return result


def build_brief(data):
    def wan(value):
        return "待核对" if value is None else f"{value / 10000:.2f}"
    def pct(value):
        return "待核对" if value is None else f"{value:.2f}%"
    daily = data["daily_operations"]
    week = daily["previous_week"]
    date = datetime.strptime(data["target_date"], "%Y-%m-%d")
    lines = [f"公司日报：{date:%m月%d日} 周{'一二三四五六日'[date.weekday()]}（金额：万元，未扣手续费）",
             f"当日{'总营业额' if daily['status'] == 'complete' else '已知营业额（数据待核对）'}：{wan(daily['known_income'])}；较上周同日 {week['date']}：{pct(week['change_pct'])}，增减 {wan(week['change'])}。" + ("仅已知数据比较，门店及平台覆盖可能不同。" if not week['complete'] else ""),
             f"当月营业额：{wan(data['curr_month_total'])}（权限内历史门店已入库数据）；目标：{data['month_target'] if data['month_target'] is not None else '未配置'}；有目标门店完成率：{pct(data['completion_rate'])}。",
             f"较上月同期：{pct(data['month_change_pct'])}（上月同期 {wan(data['last_month_total'])}）。",
             f"累计投币/出货：{data['operating_ratios']['coin_out_ratio'] if data['operating_ratios']['coin_out_ratio'] is not None else '待核对'}∶1（在营门店已入库数据，正常区间未配置）。",
             "重点新店（月累计 / 单日）："]
    lines.extend(f"{v['venue']}：{wan(v['monthly_income'])} / {wan(v['daily_income'])}" for v in data['focus_stores'])
    lines.append("区域：完成率 / 货款比（仅正目标门店）")
    lines.extend(f"{r['region']}：{pct(r['pct'])} / {pct(r['payment_pct'])}" for r in data['region_completion'])
    lines.append("内地在营门店本月营业额前三名（元，未扣手续费）：")
    lines.extend(f"{name}：{amount:,.2f}" for name, amount in data['top_cn'])
    if data.get('data_issues'):
        lines.append("注：存在快照或历史区间待核对项，以上为已入库统计。")
    return "\n".join(lines)
