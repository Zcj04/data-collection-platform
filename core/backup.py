# -*- coding: utf-8 -*-
"""SQLite 与 Fernet 密钥的成对备份、校验和保留策略。"""

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from cryptography.fernet import Fernet

from core.config import get as config_get


logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


class BackupError(RuntimeError):
    """备份无法创建或无法通过完整性校验。"""


def _configured_root() -> Path:
    configured = str(config_get("backup.directory", "data/backups/automatic")).strip()
    root = Path(configured)
    return root if root.is_absolute() else _PROJECT_ROOT / root


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quick_check(database_path: Path) -> str:
    try:
        connection = sqlite3.connect(str(database_path))
        try:
            row = connection.execute("PRAGMA quick_check").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise BackupError("备份数据库无法打开或已损坏") from error
    result = str(row[0]) if row else ""
    if result.casefold() != "ok":
        raise BackupError("备份数据库完整性校验失败")
    return result


def _file_manifest(path: Path) -> Dict[str, Any]:
    return {
        "name": path.name,
        "size": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _verify_credential_key(database_path: Path, key_path: Path) -> None:
    try:
        cipher = Fernet(key_path.read_bytes())
    except Exception as error:
        raise BackupError("备份中的 Fernet 密钥无效") from error
    connection = sqlite3.connect(str(database_path))
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='credentials'"
        ).fetchone()
        if not exists:
            return
        rows = connection.execute(
            "SELECT encrypted_value FROM credentials WHERE status='active'"
        ).fetchall()
        for row in rows:
            cipher.decrypt(str(row[0]).encode("ascii"))
    except Exception as error:
        raise BackupError("Fernet 密钥无法解密备份中的活动凭证") from error
    finally:
        connection.close()


def create_backup(
    database_path: str,
    key_file: str,
    *,
    backup_root: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Path:
    """用 SQLite 在线备份创建数据库与密钥的原子备份目录。"""
    database = Path(database_path)
    key = Path(key_file)
    if not database.is_file():
        raise BackupError("数据库文件不存在，无法备份")
    if not key.is_file():
        raise BackupError("Fernet 密钥不存在，无法创建可恢复备份")

    try:
        Fernet(key.read_bytes())
    except Exception as error:
        raise BackupError("Fernet 密钥格式无效") from error

    root = Path(backup_root) if backup_root else _configured_root()
    root.mkdir(parents=True, exist_ok=True)
    timestamp = (now or datetime.now().astimezone()).astimezone()
    bundle_name = "backup-%s-%s" % (
        timestamp.strftime("%Y%m%d-%H%M%S"),
        uuid.uuid4().hex[:8],
    )
    temporary = root / (".backup-tmp-" + uuid.uuid4().hex)
    temporary.mkdir()
    destination = root / bundle_name
    try:
        backup_database = temporary / "app.db"
        source_connection = sqlite3.connect(str(database))
        destination_connection = sqlite3.connect(str(backup_database))
        try:
            source_connection.backup(destination_connection)
        except sqlite3.Error as error:
            raise BackupError("SQLite 在线备份失败") from error
        finally:
            destination_connection.close()
            source_connection.close()

        backup_key = temporary / "fernet.key"
        shutil.copyfile(key, backup_key)
        quick_check = _quick_check(backup_database)
        manifest = {
            "version": 1,
            "created_at": timestamp.isoformat(),
            "database_quick_check": quick_check,
            "files": {
                "database": _file_manifest(backup_database),
                "fernet_key": _file_manifest(backup_key),
            },
        }
        manifest_path = temporary / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(temporary), str(destination))
        verify_backup(destination)
        return destination
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        raise


def verify_backup(bundle_path: os.PathLike[str] | str) -> Dict[str, Any]:
    """验证清单、文件校验和、SQLite 完整性和 Fernet 密钥格式。"""
    bundle = Path(bundle_path)
    manifest_path = bundle / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BackupError("备份清单缺失或格式无效") from error
    if manifest.get("version") != 1 or not isinstance(manifest.get("files"), dict):
        raise BackupError("备份清单版本或文件列表无效")

    verified: Dict[str, Path] = {}
    for field in ("database", "fernet_key"):
        item = manifest["files"].get(field)
        if not isinstance(item, dict) or not item.get("name"):
            raise BackupError("备份清单缺少必要文件")
        path = bundle / str(item["name"])
        if path.parent != bundle or not path.is_file():
            raise BackupError("备份文件缺失或路径无效")
        if path.stat().st_size != item.get("size") or _sha256(path) != item.get("sha256"):
            raise BackupError("备份文件校验和不匹配")
        verified[field] = path

    _quick_check(verified["database"])
    _verify_credential_key(verified["database"], verified["fernet_key"])
    return manifest


def latest_verified_backup(
    *, backup_root: Optional[str] = None
) -> Optional[Tuple[Path, datetime]]:
    """返回最新且通过校验的自动备份；损坏目录不会被当作健康备份。"""
    root = Path(backup_root) if backup_root else _configured_root()
    if not root.is_dir():
        return None
    candidates = []
    for bundle in root.iterdir():
        if not bundle.is_dir() or not bundle.name.startswith("backup-"):
            continue
        try:
            manifest = verify_backup(bundle)
            created_at = datetime.fromisoformat(str(manifest["created_at"]))
            if created_at.tzinfo is None:
                created_at = created_at.astimezone()
            candidates.append((bundle, created_at))
        except (BackupError, KeyError, TypeError, ValueError):
            logger.warning("忽略未通过校验的自动备份目录：%s", bundle.name)
    return max(candidates, key=lambda item: item[1]) if candidates else None


def prune_backups(
    *,
    backup_root: Optional[str] = None,
    retention_days: int = 30,
    now: Optional[datetime] = None,
) -> int:
    """只删除已验证且超过保留期的自动备份目录。"""
    root = Path(backup_root) if backup_root else _configured_root()
    if not root.is_dir():
        return 0
    cutoff = (now or datetime.now().astimezone()).astimezone() - timedelta(
        days=max(1, int(retention_days))
    )
    removed = 0
    for bundle in root.iterdir():
        if not bundle.is_dir() or not bundle.name.startswith("backup-"):
            continue
        try:
            manifest = verify_backup(bundle)
            created_at = datetime.fromisoformat(str(manifest["created_at"]))
            if created_at.tzinfo is None:
                created_at = created_at.astimezone()
        except (BackupError, KeyError, TypeError, ValueError):
            continue
        if created_at < cutoff:
            shutil.rmtree(bundle)
            removed += 1
    return removed


def ensure_daily_backup(
    database_path: str,
    key_file: str,
    *,
    backup_root: Optional[str] = None,
    retention_days: int = 30,
    now: Optional[datetime] = None,
) -> Tuple[Path, bool]:
    """当天已有有效备份时复用，否则创建一份并执行保留策略。"""
    current = (now or datetime.now().astimezone()).astimezone()
    latest = latest_verified_backup(backup_root=backup_root)
    if latest and latest[1].astimezone().date() == current.date():
        prune_backups(
            backup_root=backup_root,
            retention_days=retention_days,
            now=current,
        )
        return latest[0], False
    bundle = create_backup(
        database_path,
        key_file,
        backup_root=backup_root,
        now=current,
    )
    prune_backups(
        backup_root=backup_root,
        retention_days=retention_days,
        now=current,
    )
    return bundle, True


class BackupScheduler:
    """定期检查并确保每天至少有一份通过校验的成对备份。"""

    def __init__(self, database_path: str, key_file: str) -> None:
        self.database_path = database_path
        self.key_file = key_file
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_error = ""

    def _run_once(self) -> None:
        retention_days = int(config_get("backup.retention_days", 30))
        bundle, created = ensure_daily_backup(
            self.database_path,
            self.key_file,
            retention_days=retention_days,
        )
        self.last_error = ""
        if created:
            logger.info("已创建每日成对备份：%s", bundle.name)

    def _loop(self) -> None:
        interval = max(60, int(config_get("backup.interval_seconds", 3600)))
        while not self._stop_event.is_set():
            try:
                self._run_once()
            except Exception as error:
                self.last_error = str(error)
                logger.exception("每日备份检查失败")
                try:
                    from core.alerts import record_alert

                    record_alert(
                        "backup.failed",
                        "critical",
                        "每日数据库备份失败",
                        str(error),
                        dedupe_key="backup.failed:%s" % datetime.now().date().isoformat(),
                    )
                except Exception:
                    logger.exception("记录备份失败告警时出错")
            self._stop_event.wait(interval)

    def start(self) -> None:
        if not bool(config_get("backup.enabled", True)):
            logger.warning("自动备份已被配置关闭")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="daily-backup",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
