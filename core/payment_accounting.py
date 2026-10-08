# -*- coding: utf-8 -*-
"""进场货款维护与月末基础货款核对。"""

import calendar
import json
from datetime import date as date_type, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Dict, Iterable, List, Optional

from core import payment_store
from core.db import get_connection
from crawlers.payment_crawler import get_match_list, match_payment_lists
from crawlers.report_summary import _get_base_df


POINT_RATE = Decimal("1.5")
POINT_METRIC_PAIRS = (
    ("StarThing积分增加", "StarThing积分减少"),
    ("鲸舰积分增加", "鲸舰积分减少"),
    ("新系统积分增加", "新系统积分减少"),
    ("芸苔积分增加", "芸苔积分减少"),
)


def _scope_set(venue_scope: Optional[Iterable[str]]) -> Optional[set[str]]:
    if venue_scope is None:
        return None
    return {str(value).strip() for value in venue_scope if str(value).strip()}


def _text(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _fallback_venues() -> List[Dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT venue FROM entry_payment_revisions "
            "UNION SELECT venue FROM daily_summary ORDER BY venue"
        ).fetchall()
        return [
            {"venue": _text(row["venue"]), "owner": "", "operating": None}
            for row in rows
            if _text(row["venue"])
        ]
    finally:
        conn.close()


def get_venue_roster() -> Dict:
    """读取数据看板标准门店与负责人状态；失败时保留本地历史门店。"""
    try:
        dataframe, _ = _get_base_df()
        if "场地" not in dataframe.columns or "负责人" not in dataframe.columns:
            raise RuntimeError("数据看板缺少场地或负责人列")
        rows = []
        seen = set()
        for venue, owner in dataframe[["场地", "负责人"]].itertuples(index=False):
            venue_name = _text(venue)
            owner_name = _text(owner)
            if not venue_name or venue_name in seen:
                continue
            seen.add(venue_name)
            rows.append({
                "venue": venue_name,
                "owner": owner_name,
                "operating": bool(owner_name and "撤店" not in owner_name),
            })
        rows.sort(key=lambda item: (item["operating"] is not True, item["venue"]))
        return {"source": "dashboard", "available": True, "rows": rows}
    except Exception as exc:
        return {
            "source": "local_fallback",
            "available": False,
            "warning": "数据看板门店暂时不可用，当前仅显示本地历史门店",
            "error": str(exc),
            "rows": _fallback_venues(),
        }


def _latest_entry_payments() -> Dict[str, Dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT r.venue, r.revision, r.entry_date, r.amount_cents, r.note, "
            "r.actor_user_id, r.created_at "
            "FROM entry_payment_revisions r "
            "INNER JOIN ("
            "  SELECT venue, MAX(revision) AS revision "
            "  FROM entry_payment_revisions GROUP BY venue"
            ") latest ON latest.venue=r.venue AND latest.revision=r.revision"
        ).fetchall()
        return {
            row["venue"]: {
                "revision": row["revision"],
                "entry_date": row["entry_date"],
                "amount": round(row["amount_cents"] / 100, 2),
                "note": row["note"],
                "actor_user_id": row["actor_user_id"],
                "updated_at": row["created_at"],
            }
            for row in rows
        }
    finally:
        conn.close()


def list_entry_payments(venue_scope: Optional[Iterable[str]] = None) -> Dict:
    scope = _scope_set(venue_scope)
    roster = get_venue_roster()
    if scope is not None:
        roster["rows"] = [item for item in roster["rows"] if item["venue"] in scope]
    latest = _latest_entry_payments()
    if scope is not None:
        latest = {venue: entry for venue, entry in latest.items() if venue in scope}
    roster_names = {item["venue"] for item in roster["rows"]}
    rows = []
    for venue_row in roster["rows"]:
        entry = latest.get(venue_row["venue"])
        rows.append({**venue_row, "entry": entry})
    for venue in sorted(set(latest) - roster_names):
        rows.append({"venue": venue, "owner": "", "operating": None, "entry": latest[venue]})

    filled = [item for item in rows if item["entry"] is not None]
    return {
        "roster": {
            "source": roster["source"],
            "available": roster["available"],
            "warning": roster.get("warning", ""),
        },
        "summary": {
            "venue_count": len(rows),
            "operating_count": sum(item["operating"] is True for item in rows),
            "closed_count": sum(item["operating"] is False for item in rows),
            "filled_count": len(filled),
            "unfilled_count": len(rows) - len(filled),
            "total": round(sum(item["entry"]["amount"] for item in filled), 2),
        },
        "rows": rows,
    }


def _amount_to_cents(value) -> int:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError("进场货款必须是有效金额")
    if not amount.is_finite() or amount < 0:
        raise ValueError("进场货款不能小于 0")
    return int(amount * 100)


def save_entry_payment(
    venue: str,
    entry_date: str,
    amount,
    note: str = "",
    actor_user_id: str = "",
) -> Dict:
    venue_name = _text(venue)
    if not venue_name:
        raise ValueError("门店不能为空")
    try:
        datetime.strptime(entry_date, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise ValueError("进场日期格式错误，请使用 YYYY-MM-DD")
    note_text = _text(note)
    if len(note_text) > 500:
        raise ValueError("备注不能超过 500 个字符")
    amount_cents = _amount_to_cents(amount)
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    roster = get_venue_roster()
    allowed = {item["venue"] for item in roster["rows"]}
    if venue_name not in allowed:
        raise ValueError("门店不在当前标准门店清单中")

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT revision, entry_date, amount_cents, note, actor_user_id, created_at "
            "FROM entry_payment_revisions WHERE venue=? "
            "ORDER BY revision DESC LIMIT 1",
            (venue_name,),
        ).fetchone()
        if current and (
            current["entry_date"] == entry_date
            and current["amount_cents"] == amount_cents
            and current["note"] == note_text
        ):
            conn.commit()
            return {
                "venue": venue_name,
                "revision": current["revision"],
                "entry_date": current["entry_date"],
                "amount": round(current["amount_cents"] / 100, 2),
                "note": current["note"],
                "actor_user_id": current["actor_user_id"],
                "updated_at": current["created_at"],
                "unchanged": True,
            }
        revision = (current["revision"] if current else 0) + 1
        conn.execute(
            "INSERT INTO entry_payment_revisions "
            "(venue,revision,entry_date,amount_cents,note,actor_user_id,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                venue_name, revision, entry_date, amount_cents, note_text,
                actor_user_id, created_at,
            ),
        )
        saved = conn.execute(
            "SELECT created_at FROM entry_payment_revisions WHERE venue=? AND revision=?",
            (venue_name, revision),
        ).fetchone()
        conn.commit()
        return {
            "venue": venue_name,
            "revision": revision,
            "entry_date": entry_date,
            "amount": round(amount_cents / 100, 2),
            "note": note_text,
            "actor_user_id": actor_user_id,
            "updated_at": saved["created_at"],
            "unchanged": False,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def month_end_date(year: int, month: int) -> str:
    if year < 2000 or year > 2100:
        raise ValueError("年份必须在 2000-2100 之间")
    if month < 1 or month > 12:
        raise ValueError("月份必须在 1-12 之间")
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{last_day:02d}"


def get_monthly_base_payment(
    year: int,
    month: int,
    venue_scope: Optional[Iterable[str]] = None,
) -> Dict:
    scope = _scope_set(venue_scope)
    closing_date = month_end_date(year, month)
    imported = payment_store.get_date_rows(closing_date)
    info = payment_store.get_date_info(closing_date)
    roster = get_venue_roster()
    if scope is not None:
        roster["rows"] = [item for item in roster["rows"] if item["venue"] in scope]

    base_result = {
        "year": year,
        "month": month,
        "month_end_date": closing_date,
        "rule": "只读取该月最后一个自然日导入的当月累计金额",
        "roster": {
            "source": roster["source"],
            "available": roster["available"],
            "warning": roster.get("warning", ""),
        },
    }
    if not imported:
        return {
            **base_result,
            "status": "missing",
            "info": None,
            "summary": {
                "raw_count": 0,
                "mapped_count": 0,
                "raw_total": 0.0,
                "mapped_total": 0.0,
                "unmatched_count": 0,
                "unmatched_total": 0.0,
                "missing_operating_count": sum(item["operating"] is True for item in roster["rows"]),
            },
            "rows": [{**item, "amount": None, "source_names": [], "data_status": "missing"} for item in roster["rows"]],
            "unmatched": [],
        }

    try:
        match_list = get_match_list()
        mapping_available = True
        mapping_warning = ""
    except Exception as exc:
        match_list = []
        mapping_available = False
        mapping_warning = f"门店映射暂时不可用：{exc}"

    mapped_by_venue: Dict[str, Dict] = {}
    unmatched = []
    visible_imported = []
    for source_row in imported:
        item = {
            "货款店铺名": source_row["shop_name"],
            "基础货款": source_row["amount"],
        }
        matched, _, _ = match_payment_lists([item], match_list)
        if not matched:
            if scope is not None:
                continue
            unmatched.append({
                "shop_name": source_row["shop_name"],
                "amount": round(float(source_row["amount"]), 2),
            })
            continue
        venue = matched[0]["场地"]
        if scope is not None and venue not in scope:
            continue
        visible_imported.append(source_row)
        bucket = mapped_by_venue.setdefault(venue, {"amount": 0.0, "source_names": []})
        bucket["amount"] += float(source_row["amount"])
        bucket["source_names"].append(source_row["shop_name"])

    roster_names = {item["venue"] for item in roster["rows"]}
    rows = []
    for venue_row in roster["rows"]:
        mapped = mapped_by_venue.get(venue_row["venue"])
        rows.append({
            **venue_row,
            "amount": round(mapped["amount"], 2) if mapped else None,
            "source_names": mapped["source_names"] if mapped else [],
            "data_status": "present" if mapped else "missing",
        })
    for venue in sorted(set(mapped_by_venue) - roster_names):
        mapped = mapped_by_venue[venue]
        rows.append({
            "venue": venue,
            "owner": "",
            "operating": None,
            "amount": round(mapped["amount"], 2),
            "source_names": mapped["source_names"],
            "data_status": "present",
        })

    missing_operating = sum(
        item["operating"] is True and item["data_status"] == "missing"
        for item in rows
    )
    unmatched_total = round(sum(item["amount"] for item in unmatched), 2)
    mapped_total = round(sum(item["amount"] for item in rows if item["amount"] is not None), 2)
    if scope is not None:
        info = {
            "date": closing_date,
            "count": len(visible_imported),
            "total": round(sum(float(item["amount"]) for item in visible_imported), 2),
            "source_file": max((item.get("source_file") or "" for item in visible_imported), default=""),
        } if visible_imported else None
    status = "attention" if unmatched or missing_operating or not mapping_available else "complete"
    return {
        **base_result,
        "status": status,
        "mapping_available": mapping_available,
        "mapping_warning": mapping_warning,
        "info": info,
        "summary": {
            "raw_count": len(visible_imported) if scope is not None else len(imported),
            "mapped_count": sum(len(item["source_names"]) for item in rows),
            "raw_total": round(sum(float(item["amount"]) for item in (visible_imported if scope is not None else imported)), 2),
            "mapped_total": mapped_total,
            "unmatched_count": len(unmatched),
            "unmatched_total": unmatched_total,
            "missing_operating_count": missing_operating,
        },
        "rows": rows,
        "unmatched": unmatched,
    }


def _decimal(value) -> Decimal:
    try:
        number = Decimal(str(value or 0))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")
    return number if number.is_finite() else Decimal("0")


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def get_monthly_points(
    year: int,
    month: int,
    venue_scope: Optional[Iterable[str]] = None,
) -> Dict:
    """严格读取月末每日采集快照中的净积分，并统一按 1.5 元/积分换算。"""
    closing_date = month_end_date(year, month)
    scope = _scope_set(venue_scope)
    roster = get_venue_roster()
    if scope is not None:
        roster["rows"] = [item for item in roster["rows"] if item["venue"] in scope]
    conn = get_connection()
    try:
        source_rows = conn.execute(
            "SELECT venue, platform, metrics_json FROM daily_summary "
            "WHERE date=? ORDER BY venue, platform",
            (closing_date,),
        ).fetchall()
    finally:
        conn.close()

    if scope is not None:
        source_rows = [
            row for row in source_rows
            if str(row["venue"]).strip() in scope
        ]

    by_venue: Dict[str, Dict] = {}
    for source_row in source_rows:
        try:
            metrics = json.loads(source_row["metrics_json"] or "{}")
        except (TypeError, ValueError):
            metrics = {}
        increase = Decimal("0")
        decrease = Decimal("0")
        present = False
        for increase_key, decrease_key in POINT_METRIC_PAIRS:
            if increase_key in metrics or decrease_key in metrics:
                present = True
                increase += _decimal(metrics.get(increase_key))
                decrease += _decimal(metrics.get(decrease_key))
        if not present:
            continue
        bucket = by_venue.setdefault(
            _text(source_row["venue"]),
            {"increase": Decimal("0"), "decrease": Decimal("0"), "platforms": []},
        )
        bucket["increase"] += increase
        bucket["decrease"] += decrease
        platform = _text(source_row["platform"])
        if platform and platform not in bucket["platforms"]:
            bucket["platforms"].append(platform)

    roster_names = {item["venue"] for item in roster["rows"]}
    rows = []
    for venue_row in roster["rows"]:
        points = by_venue.get(venue_row["venue"])
        if points is None:
            rows.append({
                **venue_row,
                "increase": None,
                "decrease": None,
                "net_points": None,
                "amount": None,
                "platforms": [],
                "data_status": "missing",
            })
            continue
        net_points = points["increase"] - points["decrease"]
        rows.append({
            **venue_row,
            "increase": float(points["increase"]),
            "decrease": float(points["decrease"]),
            "net_points": float(net_points),
            "amount": _money(net_points * POINT_RATE),
            "platforms": sorted(points["platforms"]),
            "data_status": "present",
        })
    for venue in sorted(set(by_venue) - roster_names):
        points = by_venue[venue]
        net_points = points["increase"] - points["decrease"]
        rows.append({
            "venue": venue,
            "owner": "",
            "operating": None,
            "increase": float(points["increase"]),
            "decrease": float(points["decrease"]),
            "net_points": float(net_points),
            "amount": _money(net_points * POINT_RATE),
            "platforms": sorted(points["platforms"]),
            "data_status": "present",
        })

    present_rows = [item for item in rows if item["data_status"] == "present"]
    missing_operating = sum(
        item["operating"] is True and item["data_status"] == "missing"
        for item in rows
    )
    net_total = sum((_decimal(item["net_points"]) for item in present_rows), Decimal("0"))
    if not source_rows or not present_rows:
        status = "missing"
    elif missing_operating:
        status = "attention"
    else:
        status = "complete"
    return {
        "year": year,
        "month": month,
        "month_end_date": closing_date,
        "point_rate": float(POINT_RATE),
        "rule": "月末净积分=(各平台积分增加-积分减少)之和；1积分=1.5元",
        "status": status,
        "summary": {
            "source_row_count": len(source_rows),
            "venue_count": len(present_rows),
            "net_points": float(net_total),
            "amount": _money(net_total * POINT_RATE),
            "missing_operating_count": missing_operating,
        },
        "rows": rows,
    }


def _month_sequence(start_month: str, end_month: str) -> List[str]:
    start = datetime.strptime(start_month, "%Y-%m")
    end = datetime.strptime(end_month, "%Y-%m")
    result = []
    cursor = start
    while cursor <= end:
        result.append(cursor.strftime("%Y-%m"))
        cursor = datetime(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    return result


def _summary_months(as_of: date_type) -> List[str]:
    last_closed = as_of.replace(day=1) - timedelta(days=1)
    end_month = last_closed.strftime("%Y-%m")
    conn = get_connection()
    try:
        candidates = [
            row["month"]
            for row in conn.execute(
                "SELECT MIN(substr(date,1,7)) AS month FROM payment_imports WHERE date<=? "
                "UNION ALL SELECT MIN(substr(date,1,7)) FROM daily_summary WHERE date<=? "
                "UNION ALL SELECT MIN(substr(entry_date,1,7)) FROM entry_payment_revisions WHERE entry_date<=?",
                (last_closed.isoformat(), last_closed.isoformat(), last_closed.isoformat()),
            ).fetchall()
            if row["month"]
        ]
    finally:
        conn.close()
    if not candidates:
        return []
    start_month = min(candidates)
    if start_month > end_month:
        return []
    return _month_sequence(start_month, end_month)


def get_lifetime_summary(
    as_of_date: str | None = None,
    venue_scope: Optional[Iterable[str]] = None,
) -> Dict:
    """汇总进场、严格月末基础货款及严格月末积分；出货暂不计入。"""
    if as_of_date:
        try:
            as_of = datetime.strptime(as_of_date, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("汇总日期格式错误，请使用 YYYY-MM-DD")
    else:
        as_of = date_type.today()

    scope = _scope_set(venue_scope)
    roster = get_venue_roster()
    if scope is not None:
        roster["rows"] = [item for item in roster["rows"] if item["venue"] in scope]
    latest_entries = _latest_entry_payments()
    if scope is not None:
        latest_entries = {venue: entry for venue, entry in latest_entries.items() if venue in scope}
    months = _summary_months(as_of)
    venue_meta = {item["venue"]: dict(item) for item in roster["rows"]}
    base_by_venue: Dict[str, Dict[str, float]] = {}
    points_by_venue: Dict[str, Dict[str, float]] = {}
    monthly_rows = []
    unmatched_base_total = Decimal("0")

    for month_key in months:
        year, month = (int(part) for part in month_key.split("-"))
        base = get_monthly_base_payment(year, month, scope)
        points = get_monthly_points(year, month, scope)
        for item in base["rows"]:
            venue_meta.setdefault(item["venue"], {
                "venue": item["venue"], "owner": item.get("owner", ""),
                "operating": item.get("operating"),
            })
            if item["amount"] is not None:
                base_by_venue.setdefault(item["venue"], {})[month_key] = float(item["amount"])
        for item in points["rows"]:
            venue_meta.setdefault(item["venue"], {
                "venue": item["venue"], "owner": item.get("owner", ""),
                "operating": item.get("operating"),
            })
            if item["net_points"] is not None:
                points_by_venue.setdefault(item["venue"], {})[month_key] = float(item["net_points"])
        unmatched_base_total += _decimal(base["summary"]["unmatched_total"])
        monthly_rows.append({
            "month": month_key,
            "month_end_date": base["month_end_date"],
            "base_status": base["status"],
            "base_total": base["summary"]["mapped_total"],
            "base_unmatched_count": base["summary"]["unmatched_count"],
            "points_status": points["status"],
            "net_points": points["summary"]["net_points"],
            "points_amount": points["summary"]["amount"],
            "points_missing_operating_count": points["summary"]["missing_operating_count"],
        })

    rows = []
    for venue, meta in venue_meta.items():
        entry = latest_entries.get(venue)
        venue_base = base_by_venue.get(venue, {})
        venue_points = points_by_venue.get(venue, {})
        observed_months = sorted(set(venue_base) | set(venue_points))
        entry_month = entry["entry_date"][:7] if entry and entry.get("entry_date") else None
        start_month = entry_month or (observed_months[0] if observed_months else None)
        if meta.get("operating") is True:
            end_month = months[-1] if months else None
        else:
            end_month = observed_months[-1] if observed_months else start_month
        expected_months = (
            _month_sequence(start_month, end_month)
            if start_month and end_month and start_month <= end_month
            else []
        )
        missing_base = [month for month in expected_months if month not in venue_base]
        missing_points = [month for month in expected_months if month not in venue_points]
        base_total = sum((_decimal(value) for value in venue_base.values()), Decimal("0"))
        points_total = sum((_decimal(value) for value in venue_points.values()), Decimal("0"))
        points_amount = points_total * POINT_RATE
        entry_amount = _decimal(entry["amount"]) if entry else Decimal("0")
        known_total = entry_amount + base_total + points_amount
        issue_labels = []
        if not entry:
            issue_labels.append("缺少进场货款")
        elif not entry.get("entry_date"):
            issue_labels.append("进场日期待补")
        if missing_base:
            issue_labels.append(f"基础货款缺{len(missing_base)}月")
        if missing_points:
            issue_labels.append(f"积分缺{len(missing_points)}月")
        rows.append({
            **meta,
            "entry_amount": float(entry_amount) if entry else None,
            "entry_date": entry["entry_date"] if entry else None,
            "base_total": _money(base_total),
            "base_month_count": len(venue_base),
            "points_total": float(points_total),
            "points_amount": _money(points_amount),
            "points_month_count": len(venue_points),
            "outbound_amount": None,
            "known_total": _money(known_total),
            "expected_month_count": len(expected_months),
            "missing_base_months": missing_base,
            "missing_points_months": missing_points,
            "issues": issue_labels,
            "status": "attention" if issue_labels else "complete",
        })

    rows.sort(key=lambda item: (item.get("operating") is not True, item["venue"]))
    entry_total = sum((_decimal(item["entry_amount"]) for item in rows if item["entry_amount"] is not None), Decimal("0"))
    base_total = sum((_decimal(item["base_total"]) for item in rows), Decimal("0"))
    points_total = sum((_decimal(item["points_total"]) for item in rows), Decimal("0"))
    points_amount = points_total * POINT_RATE
    known_total = entry_total + base_total + points_amount
    return {
        "as_of_date": as_of.isoformat(),
        "through_month": months[-1] if months else None,
        "point_rate": float(POINT_RATE),
        "rule": "已知总货款=进场货款+各月最后一个自然日基础货款+各月最后一个自然日净积分×1.5；出货货款暂不计入",
        "outbound_status": "pending",
        "roster": {
            "source": roster["source"],
            "available": roster["available"],
            "warning": roster.get("warning", ""),
        },
        "summary": {
            "venue_count": len(rows),
            "month_count": len(months),
            "entry_total": _money(entry_total),
            "base_total": _money(base_total),
            "points_total": float(points_total),
            "points_amount": _money(points_amount),
            "known_total": _money(known_total),
            "incomplete_count": sum(item["status"] != "complete" for item in rows),
            "unmatched_base_total": _money(unmatched_base_total),
        },
        "months": monthly_rows,
        "rows": rows,
    }
