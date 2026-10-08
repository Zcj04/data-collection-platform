# -*- coding: utf-8 -*-
"""AI 分析师 - 数据工具集

以 OpenAI function calling 的 tools schema 暴露给模型，由 agent 编排器执行。
所有数字结论均由 Python 确定性计算，模型只负责解释与组织语言。
"""

import json
from datetime import datetime
from typing import Any, Dict, List

import numpy as np

from core.dashboard_data import get_dashboard_data
from core.db import get_connection
from core.logging import get_logger
from core.analyst.forecast import combined_forecast, detect_anomaly
from crawlers.report_summary import (
    INCOME_COLUMNS,
    WHALE_CASH_INCLUDES_KPAY_VENUES,
    kpay_of_metrics,
)

logger = get_logger("analyst.tools")

_PLATFORM_NAMES = {
    "meituan": "美团",
    "yuntai": "芸苔",
    "leyaoyao": "乐摇摇",
    "duojinbao": "多金宝",
    "jingjian": "鲸舰",
    "starthing": "starthing",
    "new_system": "新系统",
    "huilian": "汇联",
    "kpay": "kpay",
    "octopus": "八达通",
    "payment": "货款",
    "douyin": "抖音",
    "coin_exchange": "兑币机",
}


def _validate_date_range(start_date: str, end_date: str) -> None:
    for value in (start_date, end_date):
        datetime.strptime(value, "%Y-%m-%d")
    if start_date > end_date:
        raise ValueError("start_date 不能晚于 end_date")


def _row_income(metrics: Dict[str, Any]) -> float:
    """按汇总报表口径计算单行（单平台单场地）的收入：收入指标之和"""
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


def _platform_match(keyword: str, platform_id: str) -> bool:
    kw = keyword.lower()
    if kw in platform_id.lower():
        return True
    name = _PLATFORM_NAMES.get(platform_id, "")
    return kw in name.lower()


def query_summary(
    start_date: str,
    end_date: str,
    venue: str = "",
    platform: str = "",
) -> Dict[str, Any]:
    """按日期范围查询各门店/场地的收入汇总（元），支持场地/平台关键字过滤"""
    _validate_date_range(start_date, end_date)
    venue_kw = (venue or "").strip()
    platform_kw = (platform or "").strip()

    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT date, venue, platform, metrics_json FROM daily_summary "
            "WHERE date>=? AND date<=? ORDER BY date, venue",
            (start_date, end_date),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return {
            "rows": [],
            "total": 0.0,
            "dates": [],
            "note": "该日期范围内暂无采集数据",
        }

    # 每月、门店、平台仅取最后一份累计快照，禁止跨天累加累计值。
    latest = {}
    for row in rows:
        latest[(row["date"][:7], row["venue"], row["platform"])] = row
    rows = list(latest.values())
    income_by_key: Dict[tuple, float] = {}
    kpay_by_key: Dict[tuple, float] = {}
    dates: set = set()
    for r in rows:
        if venue_kw and venue_kw not in str(r["venue"]):
            continue
        if platform_kw and not _platform_match(platform_kw, r["platform"]):
            continue
        try:
            metrics = json.loads(r["metrics_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(metrics, dict):
            continue
        key = (r["date"], r["venue"])
        income_by_key[key] = income_by_key.get(key, 0.0) + _row_income(metrics)
        # 白名单门店 KPay 修正（口径与报表层一致）：长表中 KPay 在独立 platform 行，
        # 鲸舰现金已含 KPay，需按 场地+日期 扣减一次，避免重复统计。
        if r["venue"] in WHALE_CASH_INCLUDES_KPAY_VENUES:
            kpay_by_key[key] = kpay_by_key.get(key, 0.0) + kpay_of_metrics(metrics)
        dates.add(r["date"])

    if not income_by_key:
        return {
            "rows": [],
            "total": 0.0,
            "dates": sorted(dates),
            "note": "过滤条件下暂无数据",
        }

    # 无平台过滤时为「全平台收入汇总」，才应用场地级 KPay 扣减以匹配报表层；
    # 指定平台过滤时是平台定向查询（如只看 kpay/鲸舰），返回该平台原始值，不做扣减。
    if not platform_kw:
        for key, kpay in kpay_by_key.items():
            income_by_key[key] = income_by_key.get(key, 0.0) - kpay

    all_rows = [
        {"date": d, "venue": v, "income": round(val, 2)}
        for (d, v), val in income_by_key.items()
    ]
    all_rows.sort(key=lambda x: (x["date"], -x["income"]))
    total = round(sum(x["income"] for x in all_rows), 2)

    MAX_ROWS = 200
    truncated = len(all_rows) > MAX_ROWS
    note = "每月各门店平台取筛选范围内最后一份月累计快照，日期为实际采集日；total 为这些快照之和，不是起止日之间的新增收入，缺失日期不按零计算。金额单位元"
    if truncated:
        note += "；行数较多已截断，可加场地/平台条件缩小范围"
    return {
        "rows": all_rows[:MAX_ROWS],
        "total": total,
        "dates": sorted(dates),
        "truncated": truncated,
        "note": note,
    }


def dashboard_snapshot(date: str) -> Dict[str, Any]:
    """获取某日期数据大屏快照：当日/本月收入、目标完成率、门店/区域完成度、每日趋势"""
    datetime.strptime(date, "%Y-%m-%d")
    data = get_dashboard_data(date)
    if not data.get("ready"):
        return {
            "ready": False,
            "target_date": date,
            "error": data.get("error", "数据不完整，缺少前一日采集数据"),
        }

    trend = data.get("daily_trend", [])
    return {
        "ready": True,
        "target_date": date,
        "today_total": data.get("today_total"),
        "today_cn": data.get("today_cn"),
        "today_hk": data.get("today_hk"),
        "yesterday_total": data.get("yesterday_total"),
        "day_change": data.get("day_change"),
        "day_change_pct": data.get("day_change_pct"),
        "curr_month_total": data.get("curr_month_total"),
        "month_cn": data.get("month_cn"),
        "month_hk": data.get("month_hk"),
        "avg_daily": data.get("avg_daily"),
        "completion_rate": data.get("completion_rate"),
        "month_target": data.get("month_target"),
        "month_change": data.get("month_change"),
        "month_change_pct": data.get("month_change_pct"),
        "top_cn": data.get("top_cn", []),
        "top_hk": data.get("top_hk", []),
        "top_today_gain": data.get("top_today_gain", []),
        "regions": data.get("regions", {}),
        "store_completion": data.get("store_completion", []),
        "region_completion": data.get("region_completion", []),
        "daily_trend": trend[-15:],
    }


TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "query_summary",
            "description": (
                "查询日期范围内每月各门店平台最后一份月累计收入快照（元），不跨天累加累计值。"
                "可按场地或平台过滤。不是区间新增收入；返回日期可能不同，比较时必须说明快照截至日。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {
                        "type": "string",
                        "description": "开始日期，格式 YYYY-MM-DD",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "结束日期，格式 YYYY-MM-DD",
                    },
                    "venue": {
                        "type": "string",
                        "description": "场地关键字（可省略），如 香港、深圳、广州 或具体门店名",
                    },
                    "platform": {
                        "type": "string",
                        "description": "平台标识或名称（可省略），如 meituan/美团、kpay、douyin/抖音",
                    },
                },
                "required": ["start_date", "end_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dashboard_snapshot",
            "description": (
                "获取指定日期的经营数据快照：当日/本月收入、环比变化、月度目标完成率、"
                "门店与区域完成度、TOP 排行、每日趋势。适合回答经营概况、目标达成类问题。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "日期，格式 YYYY-MM-DD",
                    }
                },
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forecast",
            "description": (
                "预测经营数据：本月收入完成率推演（本月累计+日均×剩余天数）"
                "以及未来数月收入趋势预测。适合回答“本月能完成多少”“预测下月收入”类问题。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "预测基准日期，格式 YYYY-MM-DD",
                    },
                    "horizon": {
                        "type": "integer",
                        "description": "预测未来几个月，默认 3",
                    },
                },
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "detect_anomaly",
            "description": (
                "检测指定采集日各门店单日收入的异常波动（与本店本月其他采集日对比，"
                "按均值±threshold 倍标准差判定）。适合回答“最近一天哪些门店数据异常”类问题。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "采集日，格式 YYYY-MM-DD",
                    },
                    "threshold": {
                        "type": "number",
                        "description": "Z 值阈值，默认 2.0",
                    },
                },
                "required": ["date"],
            },
        },
    },
]


def _forecast_tool(date: str, horizon: int = 3) -> Dict[str, Any]:
    return combined_forecast(date, horizon=horizon)


def _anomaly_tool(date: str, threshold: float = 2.0) -> Dict[str, Any]:
    return detect_anomaly(date, threshold=threshold)


TOOL_FUNCS = {
    "query_summary": query_summary,
    "dashboard_snapshot": dashboard_snapshot,
    "forecast": _forecast_tool,
    "detect_anomaly": _anomaly_tool,
}


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.ndarray):
        return [_jsonable(v) for v in value.tolist()]
    return value


def execute_tool(name: str, arguments: str) -> str:
    """执行工具并返回 JSON 字符串结果（供模型回填 tool message）"""
    try:
        args = json.loads(arguments or "{}")
        if not isinstance(args, dict):
            args = {}
    except json.JSONDecodeError:
        args = {}
    func = TOOL_FUNCS.get(name)
    if func is None:
        return json.dumps({"error": f"未知工具：{name}"}, ensure_ascii=False)
    try:
        result = func(**args)
    except Exception as e:  # noqa: BLE001 - 工具异常需完整返回给模型
        logger.warning("工具 %s 执行失败: %s", name, e)
        return json.dumps(
            {"error": f"工具执行失败：{str(e)[:200]}"},
            ensure_ascii=False,
        )
    return json.dumps(_jsonable(result), ensure_ascii=False)
