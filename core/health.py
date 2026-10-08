# -*- coding: utf-8 -*-
"""部署探针：区分进程存活与可安全接流量。"""

import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet

from core import backup as backup_service
from core import credential_manager, db
from core.config import get as config_get
from core.process_lock import is_single_process_lock_held


logger = logging.getLogger(__name__)
_backup_cache_lock = threading.Lock()
_backup_cache: Dict[str, Any] = {"checked_at": 0.0, "latest": None}


def _latest_backup(backup_root: Optional[str]):
    if backup_root is not None:
        return backup_service.latest_verified_backup(backup_root=backup_root)
    cache_seconds = max(1, int(config_get("backup.health_cache_seconds", 60)))
    with _backup_cache_lock:
        if time.monotonic() - float(_backup_cache["checked_at"]) <= cache_seconds:
            return _backup_cache["latest"]
        latest = backup_service.latest_verified_backup()
        _backup_cache.update({"checked_at": time.monotonic(), "latest": latest})
        return latest


def readiness_status(
    *,
    database_path: Optional[str] = None,
    key_file: Optional[str] = None,
    backup_root: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """检查本进程拥有锁、SQLite 可读、密钥有效且最近备份不过期。"""
    database = database_path or db.DB_PATH
    key = key_file or credential_manager.KEY_FILE
    current = (now or datetime.now().astimezone()).astimezone()
    checks: Dict[str, Dict[str, Any]] = {}

    try:
        database_file = Path(database)
        if not database_file.is_file():
            raise sqlite3.OperationalError("database missing")
        connection = sqlite3.connect(database_file.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            connection.execute("SELECT 1").fetchone()
        finally:
            connection.close()
        checks["database"] = {"status": "ok"}
    except sqlite3.Error:
        logger.exception("就绪检查：SQLite 不可用")
        checks["database"] = {"status": "failed"}

    checks["process_lock"] = {
        "status": "ok" if is_single_process_lock_held(database) else "failed"
    }

    try:
        Fernet(Path(key).read_bytes())
        backup_service._verify_credential_key(Path(database), Path(key))
        checks["credential_key"] = {"status": "ok"}
    except Exception:
        logger.exception("就绪检查：Fernet 密钥不可用")
        checks["credential_key"] = {"status": "failed"}

    if not bool(config_get("backup.enabled", True)):
        checks["backup"] = {"status": "disabled"}
    else:
        latest = _latest_backup(backup_root)
        if latest is None:
            checks["backup"] = {"status": "missing"}
        else:
            age_hours = max(0.0, (current - latest[1].astimezone()).total_seconds() / 3600)
            max_age_hours = max(1, int(config_get("backup.max_age_hours", 36)))
            checks["backup"] = {
                "status": "ok" if age_hours <= max_age_hours else "stale",
                "age_hours": round(age_hours, 1),
            }

    ready = all(item["status"] == "ok" for item in checks.values())
    return {"status": "ready" if ready else "not_ready", "checks": checks}
