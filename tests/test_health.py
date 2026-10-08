# -*- coding: utf-8 -*-
"""就绪探针只在恢复链路完整时返回 ready。"""

import sqlite3
from datetime import datetime, timezone

from cryptography.fernet import Fernet

from core.backup import create_backup
from core.health import readiness_status
from core.process_lock import acquire_single_process_lock, release_single_process_lock


def test_readiness_requires_lock_database_key_and_fresh_backup(tmp_path):
    database = tmp_path / "app.db"
    key_file = tmp_path / ".fernet_key"
    backup_root = tmp_path / "backups"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    key_file.write_bytes(Fernet.generate_key())
    now = datetime(2026, 9, 1, 8, tzinfo=timezone.utc)
    create_backup(
        str(database),
        str(key_file),
        backup_root=str(backup_root),
        now=now,
    )

    acquire_single_process_lock(str(database))
    try:
        result = readiness_status(
            database_path=str(database),
            key_file=str(key_file),
            backup_root=str(backup_root),
            now=now,
        )
        assert result["status"] == "ready"
        assert set(result["checks"]) == {
            "database", "process_lock", "credential_key", "backup"
        }
    finally:
        release_single_process_lock(str(database))

    result = readiness_status(
        database_path=str(database),
        key_file=str(key_file),
        backup_root=str(backup_root),
        now=now,
    )
    assert result["status"] == "not_ready"
    assert result["checks"]["process_lock"]["status"] == "failed"


def test_readiness_rejects_stale_or_tampered_backup(tmp_path):
    database = tmp_path / "app.db"
    key_file = tmp_path / ".fernet_key"
    backup_root = tmp_path / "backups"
    sqlite3.connect(database).close()
    key_file.write_bytes(Fernet.generate_key())
    old = datetime(2026, 8, 1, 8, tzinfo=timezone.utc)
    bundle = create_backup(
        str(database), str(key_file), backup_root=str(backup_root), now=old
    )
    acquire_single_process_lock(str(database))
    try:
        stale = readiness_status(
            database_path=str(database),
            key_file=str(key_file),
            backup_root=str(backup_root),
            now=datetime(2026, 9, 1, 8, tzinfo=timezone.utc),
        )
        assert stale["checks"]["backup"]["status"] == "stale"

        with (bundle / "app.db").open("ab") as handle:
            handle.write(b"tampered")
        damaged = readiness_status(
            database_path=str(database),
            key_file=str(key_file),
            backup_root=str(backup_root),
            now=old,
        )
        assert damaged["checks"]["backup"]["status"] == "missing"
    finally:
        release_single_process_lock(str(database))
