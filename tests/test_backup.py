# -*- coding: utf-8 -*-
"""SQLite 与 Fernet 密钥必须成对、可验证地备份。"""

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

from core.backup import (
    BackupError,
    create_backup,
    ensure_daily_backup,
    prune_backups,
    verify_backup,
)


def _source_files(tmp_path):
    database = tmp_path / "source" / "app.db"
    key_file = tmp_path / "source" / ".fernet_key"
    database.parent.mkdir()
    key_file.write_bytes(Fernet.generate_key())
    cipher = Fernet(key_file.read_bytes())
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO records(value) VALUES ('需要恢复的数据')")
        connection.execute(
            "CREATE TABLE credentials (encrypted_value TEXT, status TEXT)"
        )
        connection.execute(
            "INSERT INTO credentials VALUES (?, 'active')",
            (cipher.encrypt(b'{\"account\":\"recoverable\"}').decode("ascii"),),
        )
        connection.commit()
    finally:
        connection.close()
    return database, key_file


def test_create_backup_copies_database_and_matching_key(tmp_path):
    database, key_file = _source_files(tmp_path)
    root = tmp_path / "automatic"

    bundle = create_backup(str(database), str(key_file), backup_root=str(root))
    manifest = verify_backup(bundle)

    assert manifest["database_quick_check"] == "ok"
    assert (bundle / "fernet.key").read_bytes() == key_file.read_bytes()
    connection = sqlite3.connect(bundle / "app.db")
    try:
        assert connection.execute("SELECT value FROM records").fetchone()[0] == "需要恢复的数据"
    finally:
        connection.close()


@pytest.mark.parametrize("filename", ["app.db", "fernet.key"])
def test_verify_backup_rejects_tampered_files(tmp_path, filename):
    database, key_file = _source_files(tmp_path)
    bundle = create_backup(
        str(database),
        str(key_file),
        backup_root=str(tmp_path / "automatic"),
    )
    with (bundle / filename).open("ab") as handle:
        handle.write(b"tampered")

    with pytest.raises(BackupError, match="校验和"):
        verify_backup(bundle)


def test_verify_backup_rejects_valid_but_mismatched_key(tmp_path):
    database, key_file = _source_files(tmp_path)
    bundle = create_backup(
        str(database),
        str(key_file),
        backup_root=str(tmp_path / "automatic"),
    )
    wrong_key = Fernet.generate_key()
    (bundle / "fernet.key").write_bytes(wrong_key)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["fernet_key"].update(
        size=len(wrong_key),
        sha256=hashlib.sha256(wrong_key).hexdigest(),
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BackupError, match="无法解密"):
        verify_backup(bundle)


def test_daily_backup_is_idempotent_and_retention_is_scoped(tmp_path):
    database, key_file = _source_files(tmp_path)
    root = tmp_path / "automatic"
    old_time = datetime(2026, 7, 1, 8, tzinfo=timezone.utc)
    current = datetime(2026, 9, 1, 8, tzinfo=timezone.utc)
    old_bundle = create_backup(
        str(database), str(key_file), backup_root=str(root), now=old_time
    )
    manual = root / "manual-keep"
    manual.mkdir()
    (manual / "note.txt").write_text("不得删除", encoding="utf-8")
    invalid = root / "backup-20200101-000000-invalid"
    invalid.mkdir()
    (invalid / "manifest.json").write_text(json.dumps({"version": 1}), encoding="utf-8")

    first, created = ensure_daily_backup(
        str(database),
        str(key_file),
        backup_root=str(root),
        retention_days=30,
        now=current,
    )
    second, created_again = ensure_daily_backup(
        str(database),
        str(key_file),
        backup_root=str(root),
        retention_days=30,
        now=current + timedelta(hours=2),
    )

    assert created is True
    assert created_again is False
    assert first == second
    assert not old_bundle.exists()
    assert manual.is_dir()
    assert invalid.is_dir()
    assert prune_backups(backup_root=str(root), retention_days=30, now=current) == 0
