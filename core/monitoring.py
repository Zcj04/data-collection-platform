# -*- coding: utf-8 -*-
"""会员资产监控服务。

爬虫侧只需调用 ``save_monitor_events`` 写入标准事件；页面与聚合逻辑不依赖
具体爬虫实现。数据库没有真实事件时，接口会返回明确标记的演示数据。
"""

import hashlib
import json
import math
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core.db import get_connection
from core.member_watch import member_watch


EVENT_META: Dict[str, Dict[str, str]] = {
    "points_earn": {"category": "points", "label": "积分增加", "unit": "积分"},
    "points_redeem": {"category": "points", "label": "积分兑换", "unit": "积分"},
    "points_adjust": {"category": "points", "label": "积分调整", "unit": "积分"},
    "balance_recharge": {"category": "balance", "label": "会员充值", "unit": "元"},
    "balance_grant": {"category": "balance", "label": "储值赠送", "unit": "元"},
    "balance_consume": {"category": "balance", "label": "储值消费", "unit": "元"},
    "balance_refund": {"category": "balance", "label": "储值退款", "unit": "元"},
    "balance_adjust": {"category": "balance", "label": "储值调整", "unit": "元"},
}

SOURCE_META = [
    {"id": "points", "name": "其他积分数据源", "icon": "stars", "connector_ready": False},
    {"id": "balance", "name": "多金宝会员资产（币＋分）", "icon": "wallet", "connector_ready": True},
]

DEMO_VENUES = ["深圳F店", "深圳M店", "深圳G店", "深圳L店"]

STATISTICAL_MIN_SAMPLE_SIZE = 20
ALERT_LEVEL_ORDER = {"critical": 0, "high": 1, "medium": 2, "watch": 3}
ASSET_GROUP_META = {
    "coin": {"label": "币类 / 可消费资产"},
    "points": {"label": "积分类"},
    "other": {"label": "待分类"},
}


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if not value:
        raise ValueError("occurred_at 不能为空")
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("occurred_at 必须为 ISO 日期时间") from exc


def _external_id(event: Dict[str, Any]) -> str:
    explicit = str(event.get("external_id") or "").strip()
    if explicit:
        return explicit
    stable = "|".join(
        str(event.get(k) or "")
        for k in ("source", "occurred_at", "venue", "member_ref", "event_type", "amount")
    )
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:32]


def normalize_monitor_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """校验并标准化一条爬虫事件，便于不同来源复用同一写入契约。"""
    event_type = str(event.get("event_type") or "").strip()
    if event_type not in EVENT_META:
        raise ValueError("不支持的 event_type: %s" % event_type)
    source = str(event.get("source") or EVENT_META[event_type]["category"]).strip()
    occurred_at = _parse_datetime(event.get("occurred_at"))
    try:
        amount = float(event.get("amount", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("amount 必须为数字") from exc
    balance_after = event.get("balance_after")
    if balance_after not in (None, ""):
        try:
            balance_after = float(balance_after)
        except (TypeError, ValueError) as exc:
            raise ValueError("balance_after 必须为数字或空值") from exc
    else:
        balance_after = None
    raw = event.get("raw", event.get("raw_json", {}))
    if isinstance(raw, str):
        try:
            json.loads(raw)
            raw_json = raw
        except json.JSONDecodeError:
            raw_json = json.dumps({"value": raw}, ensure_ascii=False)
    else:
        raw_json = json.dumps(raw or {}, ensure_ascii=False, default=str)
    normalized = {
        "source": source,
        "occurred_at": occurred_at.isoformat(sep=" ", timespec="seconds"),
        "venue": str(event.get("venue") or "").strip(),
        "member_ref": str(event.get("member_ref") or "").strip(),
        "event_type": event_type,
        "amount": amount,
        "balance_after": balance_after,
        "operator": str(event.get("operator") or "").strip(),
        "raw_json": raw_json,
    }
    normalized["external_id"] = _external_id({**event, **normalized})
    return normalized


def save_monitor_events(events: Iterable[Dict[str, Any]]) -> int:
    """幂等写入爬虫事件，返回本批成功标准化并写入的事件数。"""
    rows = [normalize_monitor_event(event) for event in events]
    if not rows:
        return 0
    conn = get_connection()
    try:
        conn.executemany(
            """
            INSERT INTO monitor_events (
                source, external_id, occurred_at, venue, member_ref,
                event_type, amount, balance_after, operator, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, external_id) DO UPDATE SET
                occurred_at=excluded.occurred_at,
                venue=excluded.venue,
                member_ref=excluded.member_ref,
                event_type=excluded.event_type,
                amount=excluded.amount,
                balance_after=excluded.balance_after,
                operator=excluded.operator,
                raw_json=excluded.raw_json,
                collected_at=CURRENT_TIMESTAMP
            """,
            [
                (
                    row["source"], row["external_id"], row["occurred_at"], row["venue"],
                    row["member_ref"], row["event_type"], row["amount"],
                    row["balance_after"], row["operator"], row["raw_json"],
                )
                for row in rows
            ],
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def begin_monitor_sync(source: str, category: str, start_date: str, end_date: str) -> str:
    """创建一条同步运行记录并返回 ID。"""
    run_id = str(uuid.uuid4())
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO monitor_sync_runs
                (id, source, category, start_date, end_date, status, started_at)
            VALUES (?, ?, ?, ?, ?, 'running', ?)
            """,
            (run_id, source, category, start_date, end_date, datetime.now()),
        )
        conn.commit()
        return run_id
    finally:
        conn.close()


def finish_monitor_sync(
    run_id: str,
    status: str,
    stores_total: int = 0,
    stores_succeeded: int = 0,
    event_count: int = 0,
    error_count: int = 0,
    error_msg: str = "",
) -> None:
    if status not in ("success", "partial", "failed"):
        raise ValueError("无效的同步状态: %s" % status)
    conn = get_connection()
    try:
        conn.execute(
            """
            UPDATE monitor_sync_runs
            SET status=?, stores_total=?, stores_succeeded=?, event_count=?,
                error_count=?, error_msg=?, finished_at=?
            WHERE id=?
            """,
            (
                status, int(stores_total), int(stores_succeeded), int(event_count),
                int(error_count), str(error_msg or "")[:1000], datetime.now(), run_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_latest_monitor_syncs() -> Dict[str, Dict[str, Any]]:
    """返回每个资产类别最近一次同步，不包含凭证或会员数据。"""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT source, category, start_date, end_date, status,
                   stores_total, stores_succeeded, event_count, error_count,
                   started_at, finished_at
            FROM monitor_sync_runs
            ORDER BY started_at DESC
            """
        ).fetchall()
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            category = str(item["category"])
            if category not in result:
                result[category] = item
        return result
    finally:
        conn.close()


def has_completed_monitor_sync(target_date: date, run_date: date) -> bool:
    """指定业务日期是否已在指定日期完成过一次同步。"""
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT 1
            FROM monitor_sync_runs
            WHERE start_date <= ? AND end_date >= ?
              AND status IN ('success', 'partial')
              AND date(started_at) = ?
            LIMIT 1
            """,
            (target_date.isoformat(), target_date.isoformat(), run_date.isoformat()),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _parse_target_date(value: Any) -> Optional[date]:
    """解析看板指定日期；空值表示使用原有时间范围。"""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError("target_date 必须为 YYYY-MM-DD") from exc


def _safe_raw_object(event: Dict[str, Any]) -> Dict[str, Any]:
    """仅在 raw/raw_json 为 JSON 对象时使用，损坏或非对象内容按空处理。"""
    raw: Any = event.get("raw_json", event.get("raw", {}))
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return {}
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _asset_context(event: Dict[str, Any]) -> Tuple[str, str, str]:
    """返回 (资产名、单位、操作渠道)，不向响应暴露其他 raw 字段。"""
    raw = _safe_raw_object(event)
    kind = str(event.get("event_type") or "")
    meta = EVENT_META.get(kind, {"category": "balance", "unit": "元"})
    explicit_asset = _first_text(
        event.get("asset_name"),
        event.get("store_value_name"),
        raw.get("asset_name"),
        raw.get("store_value_name"),
        raw.get("storeValueName"),
    )
    default_asset = "会员积分" if meta["category"] == "points" else "会员储值"
    asset_name = explicit_asset or default_asset
    explicit_unit = _first_text(event.get("unit"), raw.get("unit"))
    unit = explicit_unit or (asset_name if explicit_asset else meta["unit"])
    operation_channel = _first_text(
        event.get("operation_channel"),
        raw.get("operation_channel"),
        raw.get("operationChannel"),
    )
    return asset_name, unit, operation_channel


def _asset_group(event: Dict[str, Any], asset_name: str) -> str:
    """将平台资产名归为币类或积分类；显式标记优先于名称规则。"""
    raw = _safe_raw_object(event)
    explicit = _first_text(event.get("asset_group"), raw.get("asset_group")).lower()
    aliases = {
        "coin": "coin", "coins": "coin", "币": "coin", "币类": "coin",
        "points": "points", "point": "points", "score": "points",
        "积分": "points", "分": "points", "积分类": "points",
    }
    if explicit in aliases:
        return aliases[explicit]
    if str(event.get("event_type") or "").startswith("points_"):
        return "points"
    normalized = str(asset_name or "").strip()
    if normalized in {"余币", "弹珠", "会员储值", "储值", "余额"}:
        return "coin"
    if normalized in {"娃娃积分", "弹珠积分", "会员积分"}:
        return "points"
    if "积分" in normalized or "分值" in normalized or normalized.endswith("分"):
        return "points"
    if "币" in normalized:
        return "coin"
    return "other"


def _asset_event_label(event: Dict[str, Any]) -> str:
    """同一平台记录按资产组给出业务含义明确的展示名称。"""
    kind = str(event.get("event_type") or "")
    if kind.startswith("points_"):
        return EVENT_META[kind]["label"]
    group = event.get("asset_group")
    labels = {
        "points": {
            "balance_recharge": "积分增加", "balance_grant": "积分赠送",
            "balance_consume": "积分使用", "balance_refund": "积分退还",
            "balance_adjust": "积分调整",
        },
        "coin": {
            "balance_recharge": "币充值", "balance_grant": "币赠送",
            "balance_consume": "币消费", "balance_refund": "币退还",
            "balance_adjust": "币调整",
        },
    }
    return labels.get(str(group), {}).get(kind, EVENT_META[kind]["label"])


def _change_type_value(event: Dict[str, Any]) -> str:
    return "%s:%s" % (event["asset_group"], event["event_type"])


def _enrich_event(event: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(event)
    item["occurred_at"] = _parse_datetime(item.get("occurred_at"))
    asset_name, unit, operation_channel = _asset_context(item)
    item["asset_name"] = asset_name
    item["asset_group"] = _asset_group(item, asset_name)
    item["asset_group_label"] = ASSET_GROUP_META[item["asset_group"]]["label"]
    item["unit"] = unit
    item["operation_channel"] = operation_channel
    raw = _safe_raw_object(item)
    item["record_type"] = _first_text(raw.get("record_type"), raw.get("recordType"))
    return item


def _percentile(values: List[float], percentile: float) -> float:
    """线性插值分位数，与当前分析口径保持一致。"""
    if not values:
        raise ValueError("计算分位数时数据不能为空")
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * percentile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _demo_events(days: int, end_date: date) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    event_no = 0
    for offset in range(days):
        day = end_date - timedelta(days=days - offset - 1)
        weekday_factor = 1.22 if day.weekday() >= 5 else 1.0
        for venue_index, venue in enumerate(DEMO_VENUES):
            phase = offset + venue_index * 1.7
            earned = round((1180 + 175 * math.sin(phase / 2.3) + venue_index * 95) * weekday_factor)
            redeemed = -round((720 + 120 * math.cos(phase / 2.0) + venue_index * 60) * weekday_factor)
            recharge = round((880 + 120 * math.sin(phase / 2.6) + venue_index * 70) * weekday_factor, 2)
            consume = -round((610 + 90 * math.cos(phase / 2.8) + venue_index * 55) * weekday_factor, 2)
            specs = [
                ("points_earn", earned, 10, "消费赠送"),
                ("points_redeem", redeemed, 14, "兑换抵扣"),
                ("balance_recharge", recharge, 16, "线上充值"),
                ("balance_consume", consume, 19, "门店消费"),
            ]
            for kind, amount, hour, operator in specs:
                event_no += 1
                events.append({
                    "source": EVENT_META[kind]["category"],
                    "external_id": "demo-%s-%s" % (day.isoformat(), event_no),
                    "occurred_at": datetime.combine(day, time(hour, 8 + venue_index * 7)),
                    "venue": venue,
                    "member_ref": "138%04d%04d" % (offset + 120, event_no + 2700),
                    "event_type": kind,
                    "amount": amount,
                    "balance_after": round(abs(amount) * (1.4 + venue_index / 10), 2),
                    "operator": operator,
                })
    # 两条异常样例让告警区域在爬虫接入前可验收。
    events.append({
        "source": "points", "external_id": "demo-alert-points",
        "occurred_at": datetime.combine(end_date, time(11, 26)),
        "venue": DEMO_VENUES[1], "member_ref": "13800138000",
        "event_type": "points_adjust", "amount": -3200,
        "balance_after": 680, "operator": "后台人工调整",
    })
    events.append({
        "source": "balance", "external_id": "demo-alert-recharge",
        "occurred_at": datetime.combine(end_date, time(15, 42)),
        "venue": DEMO_VENUES[0], "member_ref": "13688886666",
        "event_type": "balance_recharge", "amount": 5000,
        "balance_after": 5288, "operator": "收银台充值",
    })
    return events


def _load_live_events(start_date: date, end_date: date) -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT source, external_id, occurred_at, venue, member_ref,
                   event_type, amount, balance_after, operator, raw_json, collected_at
            FROM monitor_events
            WHERE occurred_at >= ? AND occurred_at < ?
            ORDER BY occurred_at DESC
            """,
            (
                datetime.combine(start_date, time.min).isoformat(sep=" "),
                datetime.combine(end_date + timedelta(days=1), time.min).isoformat(sep=" "),
            ),
        ).fetchall()
        result: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["occurred_at"] = _parse_datetime(item["occurred_at"])
            result.append(item)
        return result
    finally:
        conn.close()


def _sum(events: List[Dict[str, Any]], event_type: str) -> float:
    return round(sum(float(item["amount"]) for item in events if item["event_type"] == event_type), 2)


def _event_payload(event: Dict[str, Any]) -> Dict[str, Any]:
    meta = EVENT_META[event["event_type"]]
    return {
        "occurred_at": _parse_datetime(event["occurred_at"]).isoformat(timespec="minutes"),
        "venue": event.get("venue") or "未知门店",
        "member_ref": str(event.get("member_ref") or "") or "匿名会员",
        "event_type": event["event_type"],
        "event_label": _asset_event_label(event),
        "category": meta["category"],
        "asset_group": event["asset_group"],
        "asset_group_label": event["asset_group_label"],
        "amount": round(float(event["amount"]), 2),
        "balance_after": event.get("balance_after"),
        "asset_name": event["asset_name"],
        "unit": event["unit"],
        "operation_channel": event.get("operation_channel") or "",
    }


def _statistical_profiles(
    events: List[Dict[str, Any]],
) -> Tuple[Dict[Tuple[str, str, str], Dict[str, Any]], List[Dict[str, Any]]]:
    grouped: Dict[Tuple[str, str, str], List[float]] = defaultdict(list)
    for event in events:
        business_date = _parse_datetime(event["occurred_at"]).date().isoformat()
        grouped[(business_date, event["asset_name"], event["event_type"])].append(
            abs(float(event["amount"]))
        )

    profiles: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    public_groups: List[Dict[str, Any]] = []
    for (business_date, asset_name, event_type), values in sorted(grouped.items()):
        sample_count = len(values)
        eligible = sample_count >= STATISTICAL_MIN_SAMPLE_SIZE
        threshold = _percentile(values, 0.99) if eligible else None
        reason = (
            "当日同资产、同类型样本数为 %d，可使用 P99 辅助筛查" % sample_count
            if eligible
            else "样本不足（%d < %d），未进行统计异常判定"
            % (sample_count, STATISTICAL_MIN_SAMPLE_SIZE)
        )
        profile = {
            "date": business_date,
            "asset_name": asset_name,
            "event_type": event_type,
            "event_label": EVENT_META[event_type]["label"],
            "sample_count": sample_count,
            "eligible": eligible,
            "p99": threshold,
            "reason": reason,
        }
        profiles[(business_date, asset_name, event_type)] = profile
        public_groups.append({
            **profile,
            "p99": round(threshold, 2) if threshold is not None else None,
        })
    return profiles, public_groups


def _alert_for(
    event: Dict[str, Any], statistical_profile: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    amount = float(event["amount"])
    event_type = event["event_type"]
    candidates: List[Dict[str, str]] = []

    balance_after = event.get("balance_after")
    if balance_after is not None:
        try:
            if float(balance_after) < 0:
                candidates.append({
                    "level": "critical", "title": "负余额异常", "rule": "negative_balance",
                    "reason": "期末余额为负数，需立即核对账户变更与系统规则",
                })
        except (TypeError, ValueError):
            pass

    if event_type == "balance_grant" and event.get("operation_channel") == "管理后台":
        candidates.append({
            "level": "high", "title": "管理后台赠送待复核", "rule": "backend_grant",
            "reason": "该笔赠送来自管理后台，建议核对审批依据和操作原因",
        })

    if abs(amount) > 0 and (event_type.endswith("_adjust") or event_type.endswith("_refund")):
        candidates.append({
            "level": "medium", "title": "调整或退款待复核", "rule": "adjust_or_refund",
            "reason": "该笔为非零调整或退款，建议核对业务原因",
        })

    if statistical_profile and statistical_profile["eligible"]:
        threshold = statistical_profile["p99"]
        if threshold is not None and abs(amount) > float(threshold):
            candidates.append({
                "level": "watch", "title": "当日统计偏大", "rule": "daily_asset_event_p99",
                "reason": (
                    "该笔绝对变动 %.2f 严格高于当日同资产、同类型 P99 %.2f"
                    "（样本 %d 条）"
                    % (abs(amount), float(threshold), statistical_profile["sample_count"])
                ),
            })

    if not candidates:
        return None
    candidates.sort(key=lambda item: ALERT_LEVEL_ORDER[item["level"]])
    primary = candidates[0]
    reasons = list(dict.fromkeys(item["reason"] for item in candidates))
    alert = _event_payload(event)
    alert.update({
        "level": primary["level"],
        "title": primary["title"],
        "reason": "；".join(reasons),
        "detail": "；".join(reasons),
        "rules": [item["rule"] for item in candidates],
        "_event_key": id(event),
    })
    return alert


def _top_changes(
    events: List[Dict[str, Any]],
    anomaly_by_event: Optional[Dict[int, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    anomaly_by_event = anomaly_by_event or {}
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[(event.get("venue") or "未知门店", event["asset_name"])].append(event)

    result: List[Dict[str, Any]] = []
    for (venue, asset_name), group_events in sorted(grouped.items()):
        ranked = sorted(
            group_events,
            key=lambda item: (
                abs(float(item["amount"])),
                _parse_datetime(item["occurred_at"]).isoformat(),
                str(item.get("external_id") or ""),
            ),
            reverse=True,
        )[:10]
        for rank, event in enumerate(ranked, start=1):
            payload = _event_payload(event)
            rank_reason = "%s的%s在所选日期/范围内绝对变动第 %d 名（该组共 %d 条）" % (
                venue, asset_name, rank, len(group_events)
            )
            anomaly = anomaly_by_event.get(id(event))
            payload.update({
                "rank": rank,
                "abs_amount": round(abs(float(event["amount"])), 2),
                "group_event_count": len(group_events),
                "rank_reason": rank_reason,
                "severity": anomaly["level"] if anomaly else None,
                "alert_title": anomaly["title"] if anomaly else None,
                "reason": anomaly["reason"] if anomaly else rank_reason,
            })
            result.append(payload)
    return result


def _asset_summaries(
    events: List[Dict[str, Any]],
    top_changes: List[Dict[str, Any]],
    anomalies: List[Dict[str, Any]],
    statistical_groups: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[event["asset_name"]].append(event)
    top_counts = Counter(item["asset_name"] for item in top_changes)
    anomaly_counts = Counter((item["asset_name"], item["level"]) for item in anomalies)
    stats_by_asset: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in statistical_groups:
        stats_by_asset[item["asset_name"]].append(item)

    result: List[Dict[str, Any]] = []
    for asset_name, asset_events in sorted(grouped.items()):
        categories = {EVENT_META[item["event_type"]]["category"] for item in asset_events}
        units = {item["unit"] for item in asset_events}
        level_counts = {
            level: anomaly_counts[(asset_name, level)]
            for level in ALERT_LEVEL_ORDER
        }
        amounts = [float(item["amount"]) for item in asset_events]
        result.append({
            "asset_name": asset_name,
            "asset_group": asset_events[0]["asset_group"],
            "asset_group_label": asset_events[0]["asset_group_label"],
            "unit": sorted(units)[0] if len(units) == 1 else asset_name,
            "category": sorted(categories)[0] if len(categories) == 1 else "mixed",
            "transaction_count": len(asset_events),
            "venue_count": len({item.get("venue") or "未知门店" for item in asset_events}),
            "increase_amount": round(sum(max(0.0, amount) for amount in amounts), 2),
            "decrease_amount": round(sum(abs(min(0.0, amount)) for amount in amounts), 2),
            "net_change": round(sum(amounts), 2),
            "top10_count": top_counts[asset_name],
            "anomaly_count": sum(level_counts.values()),
            "level_counts": level_counts,
            "event_type_counts": dict(sorted(Counter(
                item["event_type"] for item in asset_events
            ).items())),
            "statistical_groups": stats_by_asset.get(asset_name, []),
        })
    return result


def build_monitor_snapshot(
    events: List[Dict[str, Any]], days: int, end_date: date, mode: str,
    category: str = "all", venue: str = "", asset_group: str = "all",
    asset_name: str = "", change_type: str = "",
) -> Dict[str, Any]:
    """纯聚合函数，供 API 与测试复用。"""
    if mode not in ("live", "demo", "unsynced"):
        raise ValueError("不支持的监控模式: %s" % mode)
    if asset_group not in ("all", *ASSET_GROUP_META):
        raise ValueError("asset_group 仅支持 all、coin、points、other")
    if change_type:
        change_group, separator, event_type = change_type.partition(":")
        if (
            not separator or change_group not in ASSET_GROUP_META
            or event_type not in EVENT_META
        ):
            raise ValueError("change_type 格式不正确")
    allowed_types = set(EVENT_META)
    if category in ("points", "balance"):
        allowed_types = {key for key, meta in EVENT_META.items() if meta["category"] == category}
    start_date = end_date - timedelta(days=days - 1)
    enriched = [
        _enrich_event(item) for item in events
        if item.get("event_type") in EVENT_META
    ]
    filtered = [
        item for item in enriched
        if item["event_type"] in allowed_types and (not venue or item.get("venue") == venue)
        and (asset_group == "all" or item["asset_group"] == asset_group)
        and (not asset_name or item["asset_name"] == asset_name)
        and (not change_type or _change_type_value(item) == change_type)
        and start_date <= _parse_datetime(item["occurred_at"]).date() <= end_date
    ]
    day_map: Dict[str, Dict[str, Any]] = {}
    cursor = start_date
    while cursor <= end_date:
        day_map[cursor.isoformat()] = {
            "date": cursor.isoformat(), "points_earn": 0, "points_out": 0,
            "recharge": 0, "consume": 0,
        }
        cursor += timedelta(days=1)
    venue_map: Dict[str, Dict[str, Any]] = {}
    venue_assets: Dict[str, set] = defaultdict(set)
    for item in filtered:
        occurred_at = _parse_datetime(item["occurred_at"])
        key = occurred_at.date().isoformat()
        if key not in day_map:
            continue
        amount = float(item["amount"])
        kind = item["event_type"]
        category_name = EVENT_META[kind]["category"]
        if category_name == "points":
            day_map[key]["points_earn" if amount >= 0 else "points_out"] += abs(amount)
        elif category_name == "balance":
            day_map[key]["recharge" if amount >= 0 else "consume"] += abs(amount)
        shop = item.get("venue") or "未知门店"
        row = venue_map.setdefault(shop, {"venue": shop, "points": 0.0, "balance": 0.0, "events": 0})
        venue_assets[shop].add(item["asset_name"])
        row["events"] += 1
        if EVENT_META[kind]["category"] == "points":
            row["points"] += amount
        else:
            row["balance"] += amount

    for shop, row in venue_map.items():
        row["asset_count"] = len(venue_assets[shop])
        row["asset_names"] = sorted(venue_assets[shop])

    statistical_profiles, statistical_groups = _statistical_profiles(filtered)
    anomalies = [
        alert for alert in (
            _alert_for(item, statistical_profiles.get((
                _parse_datetime(item["occurred_at"]).date().isoformat(),
                item["asset_name"], item["event_type"],
            )))
            for item in filtered
        ) if alert
    ]
    anomalies.sort(key=lambda item: (
        ALERT_LEVEL_ORDER[item["level"]], -abs(float(item["amount"])), item["occurred_at"]
    ))
    anomaly_by_event = {int(item["_event_key"]): item for item in anomalies}
    top_changes = _top_changes(filtered, anomaly_by_event)
    for item in anomalies:
        item.pop("_event_key", None)
    asset_summaries = _asset_summaries(filtered, top_changes, anomalies, statistical_groups)
    level_counts = Counter(item["level"] for item in anomalies)
    sorted_events = sorted(filtered, key=lambda item: _parse_datetime(item["occurred_at"]), reverse=True)
    activities = []
    for item in sorted_events[:14]:
        meta = EVENT_META[item["event_type"]]
        activities.append({
            "occurred_at": _parse_datetime(item["occurred_at"]).isoformat(timespec="minutes"),
            "venue": item.get("venue") or "未知门店",
            "member_ref": str(item.get("member_ref") or "") or "匿名会员",
            "event_type": item["event_type"], "event_label": _asset_event_label(item),
            "category": meta["category"], "amount": round(float(item["amount"]), 2),
            "asset_name": item["asset_name"], "asset_group": item["asset_group"],
            "asset_group_label": item["asset_group_label"], "unit": item["unit"],
            "balance_after": item.get("balance_after"),
            "operation_channel": item.get("operation_channel") or "",
            "operator": item.get("operator") or "自动同步",
        })

    venues = sorted({item.get("venue") or "未知门店" for item in enriched})
    live_categories = {EVENT_META[item["event_type"]]["category"] for item in enriched}
    sources = []
    for source in SOURCE_META:
        connected = mode == "live" and source["id"] in live_categories
        if mode == "unsynced" and source.get("connector_ready"):
            status = "unsynced"
        elif connected:
            status = "connected"
        elif source.get("connector_ready"):
            status = "ready"
        else:
            status = "awaiting_crawler"
        sources.append({
            **source,
            "status": status,
        })
    mode_labels = {"live": "真实数据", "demo": "演示数据", "unsynced": "未同步"}
    return {
        "mode": mode,
        "mode_label": mode_labels[mode],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "period": {"days": days, "start": start_date.isoformat(), "end": end_date.isoformat()},
        "filters": {
            "category": category, "venue": venue, "venues": venues,
            "asset_group": asset_group, "asset_name": asset_name,
            "change_type": change_type,
            "asset_names": sorted({item["asset_name"] for item in enriched}),
            "asset_options": [
                {
                    "name": name,
                    "group": next(
                        item["asset_group"] for item in enriched
                        if item["asset_name"] == name
                    ),
                }
                for name in sorted({item["asset_name"] for item in enriched})
            ],
            "change_type_options": [
                {
                    "value": value,
                    "label": _asset_event_label(item),
                    "group": item["asset_group"],
                }
                for value, item in sorted(
                    {
                        _change_type_value(item): item
                        for item in enriched
                        if item["event_type"] in allowed_types
                    }.items(),
                    key=lambda pair: (pair[1]["asset_group"], _asset_event_label(pair[1])),
                )
            ],
        },
        "sources": sources,
        "summary": {
            "points_earned": round(sum(max(0, float(item["amount"])) for item in filtered if EVENT_META[item["event_type"]]["category"] == "points"), 2),
            "points_redeemed": round(sum(abs(min(0, float(item["amount"]))) for item in filtered if EVENT_META[item["event_type"]]["category"] == "points"), 2),
            "points_net": round(sum(float(item["amount"]) for item in filtered if EVENT_META[item["event_type"]]["category"] == "points"), 2),
            "recharge_amount": round(sum(max(0, float(item["amount"])) for item in filtered if EVENT_META[item["event_type"]]["category"] == "balance"), 2),
            "consumption_amount": round(sum(abs(min(0, float(item["amount"]))) for item in filtered if EVENT_META[item["event_type"]]["category"] == "balance"), 2),
            "balance_net": round(sum(float(item["amount"]) for item in filtered if EVENT_META[item["event_type"]]["category"] == "balance"), 2),
            "transaction_count": len(filtered), "alert_count": len(anomalies),
            "anomaly_count": len(anomalies),
            "venue_count": len({item.get("venue") or "未知门店" for item in filtered}),
            "store_count": len({item.get("venue") or "未知门店" for item in filtered}),
            "asset_count": len({item["asset_name"] for item in filtered}),
            "coin_transaction_count": sum(
                1 for item in filtered if item["asset_group"] == "coin"
            ),
            "points_transaction_count": sum(
                1 for item in filtered if item["asset_group"] == "points"
            ),
            "other_transaction_count": sum(
                1 for item in filtered if item["asset_group"] == "other"
            ),
            "top10_count": len(top_changes),
            "critical_count": level_counts["critical"],
            "high_count": level_counts["high"],
            "medium_count": level_counts["medium"],
            "watch_count": level_counts["watch"],
            "level_counts": {level: level_counts[level] for level in ALERT_LEVEL_ORDER},
        },
        "trend": list(day_map.values()),
        "venues": sorted(venue_map.values(), key=lambda item: item["events"], reverse=True),
        "asset_summaries": asset_summaries,
        "member_watch": member_watch(filtered),
        "top_changes": top_changes,
        "anomalies": anomalies,
        "alerts": anomalies[:8],
        "statistical_analysis": {
            "scope": "selected_day_asset_event_type",
            "method": "absolute_amount_strictly_greater_than_linear_p99",
            "minimum_sample_size": STATISTICAL_MIN_SAMPLE_SIZE,
            "note": "样本数不足 20 条的组不做统计异常判定",
            "groups": statistical_groups,
        },
        "activities": activities,
    }


def _get_monitor_syncs_for_date(target_date: date) -> Dict[str, Dict[str, Any]]:
    """返回覆盖指定日期的每类资产最新一次同步。"""
    target = target_date.isoformat()
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT source, category, start_date, end_date, status,
                   stores_total, stores_succeeded, event_count, error_count,
                   started_at, finished_at
            FROM monitor_sync_runs
            WHERE start_date <= ? AND end_date >= ?
            ORDER BY started_at DESC
            """,
            (target, target),
        ).fetchall()
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            category = str(item["category"])
            if category not in result:
                result[category] = item
        return result
    finally:
        conn.close()


def _attach_sync_metadata(
    snapshot: Dict[str, Any], syncs: Dict[str, Dict[str, Any]],
) -> None:
    for source in snapshot["sources"]:
        sync = syncs.get(source["id"])
        if not sync:
            continue
        source["status"] = (
            "connected" if sync["status"] == "success"
            else "partial" if sync["status"] == "partial"
            else "failed" if sync["status"] == "failed"
            else "syncing"
        )
        source["last_sync"] = sync.get("finished_at") or sync.get("started_at")
        source["sync_period"] = "%s ~ %s" % (sync["start_date"], sync["end_date"])
        source["event_count"] = sync.get("event_count") or 0
        source["error_count"] = sync.get("error_count") or 0
        source["stores_total"] = sync.get("stores_total") or 0
        source["stores_succeeded"] = sync.get("stores_succeeded") or 0

    relevant = list(syncs.values())
    snapshot["summary"]["stores_total"] = max(
        (int(item.get("stores_total") or 0) for item in relevant), default=0
    )
    snapshot["summary"]["stores_succeeded"] = max(
        (int(item.get("stores_succeeded") or 0) for item in relevant), default=0
    )


def get_monitor_snapshot(
    days: int = 7,
    category: str = "all",
    venue: str = "",
    target_date: Any = None,
    asset_group: str = "all",
    asset_name: str = "",
    change_type: str = "",
    venue_scope=None,
) -> Dict[str, Any]:
    days = max(1, min(int(days), 30))
    if category not in ("all", "points", "balance"):
        raise ValueError("category 仅支持 all、points、balance")
    if asset_group not in ("all", *ASSET_GROUP_META):
        raise ValueError("asset_group 仅支持 all、coin、points、other")

    parsed_target = _parse_target_date(target_date)
    scope = None if venue_scope is None else {
        str(value).strip() for value in venue_scope if str(value).strip()
    }
    if scope is not None and not scope:
        scope = set()
    if parsed_target is not None:
        end_date = parsed_target
        days = 1
        live_events = _load_live_events(parsed_target, parsed_target)
        syncs = _get_monitor_syncs_for_date(parsed_target)
        relevant_categories = {category} if category in ("points", "balance") else {"points", "balance"}
        relevant_events = [
            item for item in live_events
            if EVENT_META.get(str(item.get("event_type") or ""), {}).get("category")
            in relevant_categories
        ]
        has_completed_sync = any(
            item.get("status") in ("success", "partial")
            for sync_category, item in syncs.items()
            if sync_category in relevant_categories
        )
        mode = "live" if relevant_events or has_completed_sync else "unsynced"
        if scope is not None:
            live_events = [item for item in live_events if str(item.get("venue") or "") in scope]
        snapshot = build_monitor_snapshot(
            live_events, 1, parsed_target, mode, category, venue,
            asset_group=asset_group, asset_name=asset_name, change_type=change_type,
        )
        snapshot["target_date"] = parsed_target.isoformat()
        snapshot["is_synced"] = bool(relevant_events or has_completed_sync)
    else:
        end_date = date.today()
        live_events = _load_live_events(end_date - timedelta(days=days - 1), end_date)
        syncs = get_latest_monitor_syncs()
        has_completed_sync = any(
            item.get("status") in ("success", "partial") for item in syncs.values()
        )
        if scope is not None:
            live_events = [item for item in live_events if str(item.get("venue") or "") in scope]
        if live_events or has_completed_sync:
            snapshot = build_monitor_snapshot(
                live_events, days, end_date, "live", category, venue,
                asset_group=asset_group, asset_name=asset_name, change_type=change_type,
            )
        else:
            demo_events = _demo_events(days, end_date)
            if scope is not None:
                demo_events = [item for item in demo_events if str(item.get("venue") or "") in scope]
            snapshot = build_monitor_snapshot(
                demo_events, days, end_date, "demo", category, venue,
                asset_group=asset_group, asset_name=asset_name, change_type=change_type,
            )
        snapshot["target_date"] = None
        snapshot["is_synced"] = has_completed_sync

    if scope is not None:
        snapshot["filters"]["venues"] = sorted(scope & set(snapshot["filters"].get("venues") or []))
        visible_count = len(snapshot.get("venues") or [])
        for source in snapshot.get("sources") or []:
            source["stores_total"] = visible_count
            source["stores_succeeded"] = visible_count

    _attach_sync_metadata(snapshot, syncs)
    return snapshot
