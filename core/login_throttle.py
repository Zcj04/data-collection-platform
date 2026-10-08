# -*- coding: utf-8 -*-
"""持久化登录失败限流，不保存原始客户端地址或账号。"""

import hashlib
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

from core.config import get as config_get
from core.db import get_connection


def _now(value: Optional[datetime] = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def scope_key(client_host: str, username: str) -> str:
    normalized_host = str(client_host or "unknown").strip().casefold()
    normalized_username = str(username or "").strip().casefold()
    material = (normalized_host + "\0" + normalized_username).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def retry_after(client_host: str, username: str, *, now: Optional[datetime] = None) -> int:
    current = _now(now)
    key = scope_key(client_host, username)
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT blocked_until FROM auth_login_attempts WHERE scope_key=?",
            (key,),
        ).fetchone()
    finally:
        connection.close()
    blocked_until = _parse(row["blocked_until"]) if row else None
    if not blocked_until or blocked_until <= current:
        return 0
    return max(1, math.ceil((blocked_until - current).total_seconds()))


def record_failure(
    client_host: str,
    username: str,
    *,
    now: Optional[datetime] = None,
) -> int:
    """记录失败；达到阈值时返回应等待的秒数，否则返回 0。"""
    current = _now(now)
    max_failures = max(2, int(config_get("auth.login_max_failures", 5)))
    window_seconds = max(60, int(config_get("auth.login_window_seconds", 900)))
    block_seconds = max(60, int(config_get("auth.login_block_seconds", 900)))
    key = scope_key(client_host, username)
    connection = get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT failed_count, window_started, blocked_until "
            "FROM auth_login_attempts WHERE scope_key=?",
            (key,),
        ).fetchone()
        existing_block = _parse(row["blocked_until"]) if row else None
        if existing_block and existing_block > current:
            connection.commit()
            return max(1, math.ceil((existing_block - current).total_seconds()))

        window_started = _parse(row["window_started"]) if row else None
        if not window_started or current - window_started >= timedelta(seconds=window_seconds):
            failed_count = 1
            window_started = current
        else:
            failed_count = int(row["failed_count"] or 0) + 1
        blocked_until = (
            current + timedelta(seconds=block_seconds)
            if failed_count >= max_failures
            else None
        )
        connection.execute(
            "INSERT INTO auth_login_attempts "
            "(scope_key, failed_count, window_started, blocked_until, updated_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(scope_key) DO UPDATE SET "
            "failed_count=excluded.failed_count, window_started=excluded.window_started, "
            "blocked_until=excluded.blocked_until, updated_at=excluded.updated_at",
            (
                key,
                failed_count,
                _iso(window_started),
                _iso(blocked_until) if blocked_until else None,
                _iso(current),
            ),
        )
        cleanup_before = _iso(current - timedelta(days=7))
        connection.execute(
            "DELETE FROM auth_login_attempts WHERE updated_at<?",
            (cleanup_before,),
        )
        connection.commit()
        return block_seconds if blocked_until else 0
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def clear(client_host: str, username: str) -> None:
    connection = get_connection()
    try:
        connection.execute(
            "DELETE FROM auth_login_attempts WHERE scope_key=?",
            (scope_key(client_host, username),),
        )
        connection.commit()
    finally:
        connection.close()
