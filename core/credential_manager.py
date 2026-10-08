# -*- coding: utf-8 -*-
"""凭证管理（加密存储 + 健康检查）"""

import os
import json
from datetime import datetime
from typing import Optional, Dict

from cryptography.fernet import Fernet

from core.db import get_connection

# 密钥文件路径（基于项目根目录，避免依赖启动目录）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY_FILE = os.path.join(_PROJECT_ROOT, "credentials", ".fernet_key")

def _get_fernet() -> Fernet:
    """获取 Fernet 加密实例（自动生成/读取密钥）"""
    os.makedirs(os.path.dirname(KEY_FILE), exist_ok=True)
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, "rb") as f:
            key = f.read()
    else:
        conn = get_connection()
        try:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='credentials'").fetchone()
            if exists and conn.execute("SELECT 1 FROM credentials LIMIT 1").fetchone():
                raise RuntimeError("已有加密凭证但密钥缺失，请恢复数据库对应的密钥备份")
        finally:
            conn.close()
        key = Fernet.generate_key()
        with open(KEY_FILE, "wb") as f:
            f.write(key)
    return Fernet(key)


class CredentialManager:
    def __init__(self):
        self._fernet = _get_fernet()

    def save(self, platform: str, credential_dict: dict) -> None:
        """保存平台凭证（加密）"""
        plaintext = json.dumps(credential_dict, ensure_ascii=False)
        encrypted = self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

        conn = get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT platform FROM credentials WHERE platform=?", (platform,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE credentials SET encrypted_value=?, status='active', updated_at=? WHERE platform=?",
                    (encrypted, datetime.now(), platform)
                )
            else:
                conn.execute(
                    "INSERT INTO credentials (platform, encrypted_value, status, updated_at) VALUES (?,?,'active',?)",
                    (platform, encrypted, datetime.now())
                )
            conn.commit()
        finally:
            conn.close()

    def load(self, platform: str) -> Optional[dict]:
        """读取平台凭证（解密）"""
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT encrypted_value FROM credentials WHERE platform=? AND status='active'",
                (platform,)
            ).fetchone()
            if not row:
                return None
            plaintext = self._fernet.decrypt(row["encrypted_value"].encode("ascii")).decode("utf-8")
            return json.loads(plaintext)
        finally:
            conn.close()

    def get(self, platform: str, field: str, default: str = "") -> str:
        """读取平台凭证的单个字段"""
        cred = self.load(platform)
        if cred:
            return str(cred.get(field, default))
        return default

    def check_status(self, platform: str) -> str:
        """返回凭证状态"""
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT status FROM credentials WHERE platform=?", (platform,)
            ).fetchone()
            return row["status"] if row else "unknown"
        finally:
            conn.close()

    def mark_expired(self, platform: str):
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE credentials SET status='expired' WHERE platform=?", (platform,)
            )
            conn.commit()
        finally:
            conn.close()
