# -*- coding: utf-8 -*-
"""预测引擎：本月目标完成率推演 + 月度趋势预测 + 单日异常检测

全部为确定性统计计算（numpy），模型只负责解读，不参与算数。

数据口径说明：
- daily_summary 各采集日保存的是「当月累计值」（单位：元），
  单日收入 = 相邻两个采集日的累计差值；
- 本月完成率推演：本月累计 + 日均 × 剩余天数；
- 月度趋势：取自然月末实际值；不完整月份明确标为推演值，
  用最小二乘线性回归外推，样本少时明确标注低置信度。
"""

import calendar
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import numpy as np

from core.db import get_connection
from core.logging import get_logger
from core.targets import load_store_targets
from crawlers.report_summary import (
    INCOME_COLUMNS,
    WHALE_CASH_INCLUDES_KPAY_VENUES,
    kpay_of_metrics,
)

logger = get_logger("analyst.forecast")

def row_income(metrics: Dict[str, Any]) -> float:
    """按汇总报表口径计算单行收入：收入指标之和（元）"""
    total = 0.0
    for key, val in metrics.items():
        if key == "场地":
            continue
        if key in INCOME_COLUMNS:
            try:
                total += float(val)
            except (TypeError, ValueError):
                continue
    return total


def date_income_totals(venue_scope=None) -> Dict[str, float]:
    """返回 {采集日: 全公司收入汇总(元)}，来源 daily_summary（当月累计值）"""
    scope = None if venue_scope is None else {str(value).strip() for value in venue_scope if str(value).strip()}
    conn = get_connection()
    try:
        query = "SELECT date, venue, metrics_json FROM daily_summary"
        params = []
        if scope is not None:
            if not scope:
                return {}
            query += " WHERE venue IN (" + ",".join("?" for _ in scope) + ")"
            params.extend(sorted(scope))
        rows = conn.execute(query, tuple(params)).fetchall()
    finally:
        conn.close()
    totals: Dict[str, float] = {}
    kpay_by_key: Dict[tuple, float] = {}
    for r in rows:
        try:
            metrics = json.loads(r["metrics_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(metrics, dict):
            continue
        totals[r["date"]] = totals.get(r["date"], 0.0) + row_income(metrics)
        # 白名单门店：长表中 KPay 在独立 platform 行，需按 场地+日期 扣减一次，
        # 与报表层「鲸舰现金已含 KPay」口径一致，避免重复统计。
        if r["venue"] in WHALE_CASH_INCLUDES_KPAY_VENUES:
            key = (r["date"], r["venue"])
            kpay_by_key[key] = kpay_by_key.get(key, 0.0) + kpay_of_metrics(metrics)
    for (date, _venue), kpay in kpay_by_key.items():
        totals[date] = totals.get(date, 0.0) - kpay
    return {d: round(v, 2) for d, v in sorted(totals.items())}


def get_monthly_target(venue_scope=None, month: str = None) -> Optional[float]:
    """月度目标（元）：只读取指定业务月份的目标表。"""
    if not month:
        return None
    targets = load_store_targets(month)
    if venue_scope is not None:
        scope = {str(value).strip() for value in venue_scope if str(value).strip()}
        targets = {venue: value for venue, value in targets.items() if venue in scope}
    if targets:
        return float(sum(targets.values()))
    return None


def _days_in_month(month: str) -> int:
    year, mon = int(month[:4]), int(month[5:7])
    return calendar.monthrange(year, mon)[1]


def current_month_state(target_date: str, venue_scope=None) -> Dict[str, Any]:
    """本月累计、采集日、日均、目标完成率与月末推演"""
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    month = dt.strftime("%Y-%m")
    totals = date_income_totals(venue_scope)
    collected = [d for d in totals if month <= d <= target_date]
    if not collected:
        return {
            "available": False,
            "target_date": target_date,
            "error": "本月暂无采集数据，无法预测",
        }

    collection_date = max(collected)
    mtd = totals[collection_date]
    days_passed = int(collection_date[8:10])
    remaining = _days_in_month(collection_date[:7]) - days_passed
    target = get_monthly_target(venue_scope, month=month)
    avg_daily = mtd / days_passed if days_passed else 0.0
    projected = mtd + avg_daily * remaining
    needed_daily = ((target - mtd) / remaining) if target is not None and remaining > 0 else None

    return {
        "available": True,
        "target_date": target_date,
        "month": month,
        "collection_date": collection_date,
        "days_passed": days_passed,
        "remaining_days": remaining,
        "mtd": round(mtd, 2),
        "avg_daily": round(avg_daily, 2),
        "target": round(target, 2) if target is not None else None,
        "completion": round(mtd / target * 100, 2) if target else None,
        "projected_end": round(projected, 2),
        "projected_completion": round(projected / target * 100, 2) if target else None,
        "needed_daily": round(max(needed_daily, 0.0), 2) if needed_daily is not None else None,
        "gap": round(target - projected, 2) if target else None,
    }


def project_month_end(target_date: str, venue_scope=None) -> Dict[str, Any]:
    """本月完成率推演（主预测入口）"""
    return current_month_state(target_date, venue_scope)


def _build_monthly_series(venue_scope=None, target_date=None) -> List[Dict[str, Any]]:
    """构建月度序列：完整自然月用月末累计实际值，当前月用推演值"""
    totals = date_income_totals(venue_scope)
    by_month: Dict[str, List[tuple]] = {}
    for d, v in totals.items():
        if target_date and d > target_date:
            continue
        by_month.setdefault(d[:7], []).append((d, v))

    series: List[Dict[str, Any]] = []
    for month, entries in sorted(by_month.items()):
        entries.sort()
        last_date, last_val = entries[-1]
        if int(last_date[8:10]) == _days_in_month(month):
            series.append({"month": month, "value": last_val, "kind": "actual"})
        else:
            # 非完整月：按该月日均推演整月
            days_passed = int(last_date[8:10])
            if days_passed > 0:
                projected = last_val / days_passed * _days_in_month(month)
                series.append(
                    {
                        "month": month,
                        "value": round(projected, 2),
                        "kind": "projected",
                    }
                )
    return series


def _next_month(month: str, offset: int = 1) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    total = year * 12 + (mon - 1) + offset
    return "%04d-%02d" % (total // 12, total % 12 + 1)


def forecast_months(horizon: int = 3, venue_scope=None, target_date=None) -> Dict[str, Any]:
    """未来 N 个月趋势预测：最小二乘线性回归 + 残差置信区间"""
    if not 1 <= horizon <= 24:
        raise ValueError("预测月份数需在 1–24 之间")
    points = _build_monthly_series(venue_scope, target_date)
    if len(points) < 2:
        return {
            "available": False,
            "note": "历史月度样本不足（仅 {} 个月），暂无法做趋势预测".format(len(points)),
            "points": points,
            "forecast": [],
        }

    month_numbers = [int(p["month"][:4]) * 12 + int(p["month"][5:7]) for p in points]
    xs = np.array([m - month_numbers[0] for m in month_numbers], dtype=float)
    ys = np.array([p["value"] for p in points], dtype=float)
    slope, intercept = np.polyfit(xs, ys, 1)
    fitted = slope * xs + intercept
    std = float(np.sqrt(((ys - fitted) ** 2).mean()))
    if std <= 0:
        std = max(float(np.abs(ys).mean()) * 0.15, 1.0)

    forecasts = []
    last_month = points[-1]["month"]
    for i in range(1, horizon + 1):
        x = xs[-1] + i
        point = slope * x + intercept
        forecasts.append(
            {
                "month": _next_month(last_month, i),
                "point": round(max(point, 0.0), 2),
                "lower": round(max(point - 1.96 * std, 0.0), 2),
                "upper": round(point + 1.96 * std, 2),
            }
        )

    return {
        "available": True,
        "method": "最小二乘线性回归（确定性计算）",
        "note": "月度样本仅 {} 个月，趋势预测仅供参考".format(len(points)),
        "points": points,
        "forecast": forecasts,
    }


def detect_anomaly(target_date: str, threshold: float = 2.0, venue_scope=None) -> Dict[str, Any]:
    """对目标采集日做单日收入异常检测（按场地）

    单日收入 = 当日累计 - 前一采集日累计；基准 = 本月内该场地其他采集日
    单日值的均值 ± threshold 倍标准差。
    """
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    month = dt.strftime("%Y-%m")
    scope = None if venue_scope is None else {str(value).strip() for value in venue_scope if str(value).strip()}
    conn = get_connection()
    try:
        query = "SELECT date, venue, metrics_json FROM daily_summary WHERE date LIKE ? AND date<=?"
        params = [month + "%", target_date]
        if scope is not None:
            if not scope:
                return {"target_date": target_date, "month": month, "threshold": threshold, "count": 0, "anomalies": [], "note": "当前账号没有可见门店"}
            query += " AND venue IN (" + ",".join("?" for _ in scope) + ")"
            params.extend(sorted(scope))
        rows = conn.execute(query + " ORDER BY date", tuple(params)).fetchall()
    finally:
        conn.close()

    venue_series: Dict[str, Dict[str, float]] = {}
    venue_kpay: Dict[str, Dict[str, float]] = {}
    for r in rows:
        try:
            metrics = json.loads(r["metrics_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(metrics, dict):
            continue
        # 按 场地+日期 累加各平台收入（原逻辑为覆盖，改为累加以得到该场地当日真实收入）
        date_income = venue_series.setdefault(r["venue"], {})
        date_income[r["date"]] = date_income.get(r["date"], 0.0) + row_income(metrics)
        # 白名单门店 KPay 修正（口径与报表层一致）：按 场地+日期 扣减一次，避免重复统计
        if r["venue"] in WHALE_CASH_INCLUDES_KPAY_VENUES:
            kpay_by_date = venue_kpay.setdefault(r["venue"], {})
            kpay_by_date[r["date"]] = (
                kpay_by_date.get(r["date"], 0.0) + kpay_of_metrics(metrics)
            )
    for venue, by_date in venue_kpay.items():
        for d, kpay in by_date.items():
            venue_series[venue][d] = venue_series[venue][d] - kpay

    anomalies = []
    for venue, series in venue_series.items():
        dates = sorted(series)
        daily = []
        segment = []
        segments = []
        prev_date, prev_val = None, 0.0
        for d in dates:
            val = series[d]
            if prev_date is not None:
                current_day = datetime.strptime(d, "%Y-%m-%d").date()
                prior_day = datetime.strptime(prev_date, "%Y-%m-%d").date()
                if current_day - prior_day != timedelta(days=1):
                    # 断采后的累计差是期间增量，不能冒充某一个自然日收入。
                    if segment:
                        segments.append(segment)
                    segment = []
                    prev_date, prev_val = d, val
                    continue
                segment.append({"date": d, "value": round(val - prev_val, 2)})
            prev_date, prev_val = d, val
        if segment:
            segments.append(segment)
        if not segments or not segments[-1] or segments[-1][-1]["date"] != target_date:
            # 目标快照没有自然日前一天，不能生成该日异常。
            continue
        daily = segments[-1]

        target_idx = next(
            (i for i, x in enumerate(daily) if x["date"] == target_date),
            None,
        )
        if target_idx is None:
            continue

        values = np.array([x["value"] for x in daily], dtype=float)
        mean = float(values.mean())
        std = float(values.std())
        if std < 1e-9:
            std = max(float(np.abs(values).mean()) * 0.2, 1.0)
        tv = daily[target_idx]["value"]
        z = (tv - mean) / std if std else 0.0
        if abs(z) >= threshold:
            anomalies.append(
                {
                    "venue": venue,
                    "date": target_date,
                    "daily": round(tv, 2),
                    "baseline_mean": round(mean, 2),
                    "baseline_std": round(std, 2),
                    "z_score": round(z, 2),
                    "direction": "偏高" if tv > mean else "偏低",
                    "sample_dates": [x["date"] for x in daily],
                }
            )

    anomalies.sort(key=lambda x: -abs(x["z_score"]))
    return {
        "target_date": target_date,
        "month": month,
        "threshold": threshold,
        "count": len(anomalies),
        "anomalies": anomalies[:30],
        "note": "单日收入=当日累计-前一采集日累计；基准=该场地本月其他采集日单日值均值±{}倍标准差；出现负值通常说明当日数据缺失或上游数据被修正".format(
            threshold
        ),
    }


def combined_forecast(target_date: str, horizon: int = 3, venue_scope=None) -> Dict[str, Any]:
    """预测页面/AI 工具统一入口：本月推演 + 月度趋势"""
    projection = project_month_end(target_date, venue_scope)
    trend = forecast_months(horizon=horizon, venue_scope=venue_scope, target_date=target_date)
    return {
        "target_date": target_date,
        "projection": projection,
        "trend": trend,
        "target": get_monthly_target(venue_scope, month=target_date[:7]),
    }
