# -*- coding: utf-8 -*-
"""
货款数据模块 — 数据库存取层

存放用户按日期导入的货款明细（店铺名 + 基础货款），采集货款时优先从这里读取，
避免每次都要去修改本地 Excel 货款表。

表结构：payment_imports(date, shop_name, amount, source_file)
"""

from datetime import datetime
import json
import math
from typing import Dict, Iterable, List, Optional

from core.db import get_connection


def _record_change(conn, date, action, actor_user_id, source_file, after_rows):
    before = [dict(row) for row in conn.execute(
        "SELECT shop_name,amount,source_file FROM payment_imports WHERE date=? ORDER BY shop_name", (date,)
    )]
    conn.execute(
        "INSERT INTO payment_import_history(date,action,actor_user_id,source_file,before_json,after_json,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (date, action, actor_user_id, source_file, json.dumps(before, ensure_ascii=False),
         json.dumps(after_rows, ensure_ascii=False, allow_nan=False), datetime.now().isoformat()),
    )


def list_history(date: str, limit: int = 50) -> List[Dict]:
    """返回最近修订及完整前后快照，供审计与人工恢复核对。"""
    conn = get_connection()
    try:
        result = [dict(row) for row in conn.execute(
            "SELECT * FROM payment_import_history WHERE date=? ORDER BY id DESC LIMIT ?",
            (date, min(max(int(limit), 1), 100)),
        )]
        for row in result:
            row["before"] = json.loads(row.pop("before_json"))
            row["after"] = json.loads(row.pop("after_json"))
        return result
    finally:
        conn.close()


def replace_date_rows(date: str, rows: List[Dict], source_file: str = "", actor_user_id: str = "") -> int:
    """整日覆盖：先删除该日期旧数据，再写入新行。返回写入行数"""
    # 数据库使用 (date, shop_name) 唯一约束；在删除旧数据前显式拒绝重复，
    # 避免 INSERT OR REPLACE 静默覆盖后仍向调用方报告原始行数。
    seen = {}
    duplicates = []
    normalized_rows = []
    for row in rows:
        name = str(row.get("shop_name") or "").strip()
        key = name.casefold()
        if key in seen:
            if name not in duplicates:
                duplicates.append(name)
        else:
            seen[key] = name
        normalized = dict(row)
        normalized["shop_name"] = name
        amount = float(row["amount"])
        if not name or not math.isfinite(amount):
            raise ValueError("店名不能为空，金额必须为有限数值")
        normalized["amount"] = amount
        normalized_rows.append(normalized)
    if duplicates:
        raise ValueError("同一日期存在重复店名：%s，请合并后再导入" % "、".join(duplicates))

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _record_change(conn, date, "replace", actor_user_id, source_file,
                       [{"shop_name": r["shop_name"], "amount": r["amount"], "source_file": source_file}
                        for r in normalized_rows])
        conn.execute("DELETE FROM payment_imports WHERE date=?", (date,))
        now = datetime.now()
        for r in normalized_rows:
            conn.execute(
                "INSERT OR REPLACE INTO payment_imports "
                "(date, shop_name, amount, source_file, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    date,
                    r["shop_name"],
                    r["amount"],
                    source_file,
                    now,
                    now,
                ),
            )
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _filter_scope(rows: List[Dict], venue_scope: Optional[Iterable[str]]) -> List[Dict]:
    if venue_scope is None:
        return rows
    scope = {str(value).strip() for value in venue_scope if str(value).strip()}
    if not scope:
        return []
    try:
        from crawlers.payment_crawler import get_match_list, match_payment_lists
        match_list = get_match_list()
    except Exception:
        return []
    visible = []
    for row in rows:
        matched, _, _ = match_payment_lists(
            [{"货款店铺名": row["shop_name"], "基础货款": row["amount"]}],
            match_list,
        )
        if matched and matched[0].get("场地") in scope:
            visible.append(row)
    return visible


def get_date_rows(date: str, venue_scope: Optional[Iterable[str]] = None) -> List[Dict]:
    """读取某日导入的货款明细（按店铺名排序）"""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT date, shop_name, amount, source_file FROM payment_imports "
            "WHERE date=? ORDER BY shop_name",
            (date,),
        ).fetchall()
        return _filter_scope([dict(r) for r in rows], venue_scope)
    finally:
        conn.close()


def get_date_info(date: str, venue_scope: Optional[Iterable[str]] = None) -> Optional[Dict]:
    """某日是否有导入数据：返回 {date, count, total, source_file} 或 None"""
    if venue_scope is not None:
        rows = get_date_rows(date, venue_scope)
        if not rows:
            return None
        return {
            "date": date,
            "count": len(rows),
            "total": round(sum(float(row["amount"]) for row in rows), 2),
            "source_file": max((row.get("source_file") or "" for row in rows), default=""),
        }
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(amount),0) AS total, "
            "MAX(source_file) AS source_file FROM payment_imports WHERE date=?",
            (date,),
        ).fetchone()
        if not row or row["cnt"] == 0:
            return None
        return {
            "date": date,
            "count": row["cnt"],
            "total": round(row["total"], 2),
            "source_file": row["source_file"] or "",
        }
    finally:
        conn.close()


def list_dates(year: int, month: int, venue_scope: Optional[Iterable[str]] = None) -> List[str]:
    """某年某月有数据的日期列表（YYYY-MM-DD），按日期升序"""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT date FROM payment_imports "
            "WHERE date LIKE ? ORDER BY date",
            (f"{year:04d}-{month:02d}-%",),
        ).fetchall()
        dates = [r["date"] for r in rows]
        if venue_scope is None:
            return dates
        return [date for date in dates if get_date_rows(date, venue_scope)]
    finally:
        conn.close()


def delete_date(date: str, actor_user_id: str = "") -> int:
    """删除某日全部导入数据，返回删除行数"""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _record_change(conn, date, "delete", actor_user_id, "", [])
        cur = conn.execute("DELETE FROM payment_imports WHERE date=?", (date,))
        conn.commit()
        return cur.rowcount
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_available_years(venue_scope: Optional[Iterable[str]] = None) -> List[int]:
    """数据库中有导入数据的年份列表（降序）"""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT substr(date,1,4) AS y FROM payment_imports ORDER BY y DESC"
        ).fetchall()
        years = [int(r["y"]) for r in rows]
        if venue_scope is None:
            return years
        visible_years = []
        for year in years:
            if any(list_dates(year, month, venue_scope) for month in range(1, 13)):
                visible_years.append(year)
        return visible_years
    finally:
        conn.close()
