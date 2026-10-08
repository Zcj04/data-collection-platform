# -*- coding: utf-8 -*-
"""本地门店开业、闭店日期及按业务日期判断的展示范围。"""

import sqlite3
from datetime import datetime
from typing import Dict, Iterable, Optional

from core.db import get_connection


_UNSET = object()


def _text(value) -> str:
    return str(value or "").strip()


def _optional_date(value, label: str) -> Optional[str]:
    text = _text(value)
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}格式错误，请使用 YYYY-MM-DD") from exc


def list_lifecycle_records() -> Dict[str, Dict]:
    conn = get_connection()
    try:
        try:
            rows = conn.execute(
                "SELECT venue, opened_on, closed_on, updated_by, updated_at "
                "FROM venue_lifecycle ORDER BY venue"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return {}
            raise
        return {
            str(row["venue"]): {
                "venue": str(row["venue"]),
                "opened_on": row["opened_on"],
                "closed_on": row["closed_on"],
                "updated_by": str(row["updated_by"] or ""),
                "updated_at": row["updated_at"],
            }
            for row in rows
        }
    finally:
        conn.close()


def historical_venues() -> set[str]:
    conn = get_connection()
    try:
        try:
            rows = conn.execute(
                "SELECT DISTINCT venue FROM daily_summary WHERE venue<>'' "
                "UNION SELECT venue FROM venue_lifecycle WHERE venue<>''"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                rows = conn.execute(
                    "SELECT DISTINCT venue FROM daily_summary WHERE venue<>''"
                ).fetchall()
            else:
                raise
        return {_text(row["venue"]) for row in rows if _text(row["venue"])}
    finally:
        conn.close()


def save_lifecycle(
    venue: str,
    opened_on=None,
    closed_on=None,
    updated_by: str = "",
) -> Optional[Dict]:
    venue_name = _text(venue)
    if not venue_name:
        raise ValueError("门店不能为空")
    opened = _optional_date(opened_on, "开业日期")
    closed = _optional_date(closed_on, "闭店日期")
    if opened and closed and opened > closed:
        raise ValueError("闭店日期不能早于开业日期")

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not opened and not closed:
            conn.execute("DELETE FROM venue_lifecycle WHERE venue=?", (venue_name,))
            conn.commit()
            return None
        conn.execute(
            "INSERT INTO venue_lifecycle "
            "(venue, opened_on, closed_on, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(venue) DO UPDATE SET "
            "opened_on=excluded.opened_on, closed_on=excluded.closed_on, "
            "updated_by=excluded.updated_by, updated_at=CURRENT_TIMESTAMP",
            (venue_name, opened, closed, _text(updated_by)),
        )
        row = conn.execute(
            "SELECT venue, opened_on, closed_on, updated_by, updated_at "
            "FROM venue_lifecycle WHERE venue=?",
            (venue_name,),
        ).fetchone()
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def is_operating_on(
    venue: str,
    target_date: str,
    record=_UNSET,
    fallback_operating: bool = False,
) -> bool:
    target = _optional_date(target_date, "查询日期")
    lifecycle = (
        list_lifecycle_records().get(_text(venue))
        if record is _UNSET
        else record
    )
    if not lifecycle:
        return bool(fallback_operating)
    opened = _optional_date(lifecycle.get("opened_on"), "开业日期")
    closed = _optional_date(lifecycle.get("closed_on"), "闭店日期")
    return bool(
        target
        and (not opened or target >= opened)
        and (not closed or target <= closed)
    )


def operating_venues_on(
    target_date: str,
    candidates: Iterable[str],
    fallback_operating: Iterable[str] = (),
) -> set[str]:
    target = _optional_date(target_date, "查询日期")
    records = list_lifecycle_records()
    fallback = {_text(value) for value in fallback_operating if _text(value)}
    result = set()
    for value in candidates:
        venue = _text(value)
        if venue and is_operating_on(
            venue,
            target,
            record=records.get(venue),
            fallback_operating=venue in fallback,
        ):
            result.add(venue)
    return result
