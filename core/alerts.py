# -*- coding: utf-8 -*-
"""持久化故障事件，并可选投递到 HTTPS webhook。"""

import logging
import os
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import requests

from core.config import get as config_get
from core.db import get_connection
from utils.redaction import redact_sensitive_text


logger = logging.getLogger(__name__)


def _iso(value: Optional[datetime] = None) -> str:
    return (value or datetime.now().astimezone()).astimezone().isoformat(timespec="seconds")


def configured_webhook_url() -> str:
    return os.environ.get(
        "WORKBUDDY_ALERT_WEBHOOK_URL",
        str(config_get("alerts.webhook_url", "")),
    ).strip()


def _valid_webhook_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme.casefold() == "https" and bool(parsed.netloc)


def record_alert(
    event_type: str,
    severity: str,
    title: str,
    message: str,
    *,
    dedupe_key: str,
) -> bool:
    """持久化已脱敏事件；相同 dedupe_key 只记录一次。"""
    safe_title = redact_sensitive_text(str(title))[:200]
    safe_message = redact_sensitive_text(str(message))[:1000]
    connection = get_connection()
    try:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO alert_events "
            "(id, event_type, severity, title, message, dedupe_key, status, created_at, next_attempt_at) "
            "VALUES (?,?,?,?,?,?,'pending',?,?)",
            (
                uuid.uuid4().hex,
                str(event_type)[:80],
                str(severity)[:20],
                safe_title,
                safe_message,
                str(dedupe_key)[:200],
                _iso(),
                _iso(),
            ),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def pending_alert_count() -> int:
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM alert_events WHERE status='pending'"
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        connection.close()


def dispatch_pending(
    *,
    webhook_url: Optional[str] = None,
    now: Optional[datetime] = None,
) -> int:
    """投递到期事件；返回本次成功数量。未配置 webhook 时保留 pending。"""
    url = configured_webhook_url() if webhook_url is None else webhook_url.strip()
    if not url:
        return 0
    if not _valid_webhook_url(url):
        logger.error("告警 webhook 必须使用有效的 HTTPS URL")
        return 0

    current = (now or datetime.now().astimezone()).astimezone()
    max_attempts = max(1, int(config_get("alerts.max_attempts", 5)))
    timeout = max(1, int(config_get("alerts.timeout_seconds", 5)))
    connection = get_connection()
    try:
        rows = connection.execute(
            "SELECT id, event_type, severity, title, message, attempts, created_at "
            "FROM alert_events WHERE status='pending' AND attempts<? "
            "AND (next_attempt_at IS NULL OR next_attempt_at<=?) "
            "ORDER BY created_at LIMIT 20",
            (max_attempts, _iso(current)),
        ).fetchall()
    finally:
        connection.close()

    sent = 0
    for row in rows:
        attempts = int(row["attempts"] or 0) + 1
        payload: Dict[str, Any] = {
            "event_type": row["event_type"],
            "severity": row["severity"],
            "title": row["title"],
            "message": row["message"],
            "created_at": row["created_at"],
        }
        status = "pending"
        last_error = ""
        sent_at = None
        try:
            response = requests.post(url, json=payload, timeout=timeout)
            response.raise_for_status()
            status = "sent"
            sent_at = _iso(current)
            sent += 1
        except requests.RequestException as error:
            last_error = "webhook request failed: %s" % type(error).__name__
            if attempts >= max_attempts:
                status = "failed"
        next_attempt = _iso(current + timedelta(seconds=min(300, 5 * (2 ** attempts))))
        connection = get_connection()
        try:
            connection.execute(
                "UPDATE alert_events SET status=?, attempts=?, last_error=?, "
                "sent_at=?, next_attempt_at=? WHERE id=? AND status='pending'",
                (status, attempts, last_error, sent_at, next_attempt, row["id"]),
            )
            connection.commit()
        finally:
            connection.close()
    return sent


class AlertDispatcher:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _run(self) -> None:
        interval = max(10, int(config_get("alerts.poll_interval_seconds", 30)))
        while not self._stop_event.is_set():
            try:
                dispatch_pending()
            except Exception:
                logger.exception("告警投递检查失败")
            self._stop_event.wait(interval)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="alert-dispatcher",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None


alert_dispatcher = AlertDispatcher()
