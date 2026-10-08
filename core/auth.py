# -*- coding: utf-8 -*-
"""本地企业数据平台的账号、密码与会话存储。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from core.db import get_connection


SESSION_COOKIE_NAME = "workbuddy_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
PASSWORD_ITERATIONS = 600_000
SCOPE_ALL = "all"
SCOPE_VENUES = "venues"
SCOPE_TYPES = (SCOPE_ALL, SCOPE_VENUES)

PERMISSION_DEFINITIONS = (
    {
        "key": "portal.view",
        "label": "查看数据门户",
        "description": "查看经营总览、汇总数据和预测数据",
    },
    {
        "key": "portal.download",
        "label": "下载汇总数据",
        "description": "下载门户中的 Excel 汇总文件",
    },
    {
        "key": "boss_report.download",
        "label": "下载老板报表",
        "description": "下载每月货款比老板报表",
    },
    {
        "key": "monitoring.view",
        "label": "查看数据监控",
        "description": "查看监控概览和数据质量结果",
    },
    {
        "key": "payment.view",
        "label": "查看货款数据",
        "description": "查看每日货款和货款核算数据",
    },
    {
        "key": "payment.manage",
        "label": "管理货款数据",
        "description": "上传、修订和执行货款相关操作",
    },
    {
        "key": "admin.access",
        "label": "进入管理后台",
        "description": "使用采集、凭证、监控和货款等管理功能",
    },
    {
        "key": "users.manage",
        "label": "管理内部用户",
        "description": "创建、停用账号并调整权限或重置密码",
    },
)
PERMISSION_KEYS = tuple(item["key"] for item in PERMISSION_DEFINITIONS)
SUPERADMIN_PERMISSIONS = list(PERMISSION_KEYS)
ROLE_CUSTOM = "custom"
ROLE_TEMPLATES = (
    {
        "key": "super_admin",
        "label": "超级管理员",
        "description": "管理系统、内部账号、凭证和全部业务数据",
        "home": "系统运行总览",
        "permissions": SUPERADMIN_PERMISSIONS,
        "scope_type": SCOPE_ALL,
    },
    {
        "key": "payment_manager",
        "label": "货款管理员",
        "description": "导入、覆盖和修订货款，核对月末、进场和出货账册",
        "home": "货款管理工作台 · 每日导入",
        "permissions": [
            "portal.view",
            "portal.download",
            "payment.view",
            "payment.manage",
        ],
        "scope_type": SCOPE_ALL,
    },
    {
        "key": "finance_viewer",
        "label": "老板 / 财务查看者",
        "description": "查看经营、货款和监控结果，可下载普通数据与老板报表",
        "home": "经营与财务总览",
        "permissions": [
            "portal.view",
            "portal.download",
            "boss_report.download",
            "monitoring.view",
            "payment.view",
        ],
        "scope_type": SCOPE_ALL,
    },
    {
        "key": "store_manager",
        "label": "门店 / 区域负责人",
        "description": "查看并下载被分配门店的普通经营数据",
        "home": "指定门店数据门户",
        "permissions": ["portal.view", "portal.download"],
        "scope_type": SCOPE_VENUES,
    },
    {
        "key": "data_viewer",
        "label": "数据查看者",
        "description": "只读查看经营数据",
        "home": "数据门户",
        "permissions": ["portal.view"],
        "scope_type": SCOPE_ALL,
    },
)
ROLE_TEMPLATES_BY_KEY = {item["key"]: item for item in ROLE_TEMPLATES}

_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,49}$")
_UNSAFE_PASSWORD_PLACEHOLDER = "not-a-real-account-password"


class AuthValidationError(ValueError):
    """账号输入不符合约束。"""


class AuthConflictError(RuntimeError):
    """账号状态与请求冲突。"""


class AuthNotFoundError(LookupError):
    """目标账号不存在。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: Optional[datetime] = None) -> str:
    return (value or _now()).isoformat(timespec="seconds")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def validate_username(username: str) -> str:
    normalized = str(username or "").strip().casefold()
    if not _USERNAME_RE.fullmatch(normalized):
        raise AuthValidationError("账号需为 3-50 位字母、数字、点、下划线或短横线")
    return normalized


def validate_display_name(display_name: str) -> str:
    normalized = str(display_name or "").strip()
    if not 1 <= len(normalized) <= 50:
        raise AuthValidationError("姓名需为 1-50 个字符")
    return normalized


def validate_position(position: str) -> str:
    normalized = str(position or "").strip()
    if len(normalized) > 50:
        raise AuthValidationError("职位最多 50 个字符")
    return normalized


def validate_password(password: str) -> str:
    value = str(password or "")
    if not 10 <= len(value) <= 128:
        raise AuthValidationError("密码需为 10-128 个字符")
    if value.isspace():
        raise AuthValidationError("密码不能全部为空格")
    return value


def normalize_permissions(values: Iterable[str]) -> List[str]:
    if isinstance(values, (str, bytes)):
        raise AuthValidationError("权限格式不正确")
    selected = {str(value) for value in (values or [])}
    unknown = selected.difference(PERMISSION_KEYS)
    if unknown:
        raise AuthValidationError("包含未知权限：%s" % ", ".join(sorted(unknown)))
    if "portal.download" in selected:
        selected.add("portal.view")
    if "boss_report.download" in selected:
        selected.add("portal.view")
    if "payment.manage" in selected:
        selected.add("payment.view")
    if "admin.access" in selected:
        selected.add("portal.view")
    if "users.manage" in selected:
        selected.add("admin.access")
        selected.add("portal.view")
    if not selected:
        raise AuthValidationError("至少选择一项权限")
    return [key for key in PERMISSION_KEYS if key in selected]


def normalize_role_key(role_key: str) -> str:
    normalized = str(role_key or ROLE_CUSTOM).strip()
    if normalized == ROLE_CUSTOM or normalized in ROLE_TEMPLATES_BY_KEY:
        return normalized
    raise AuthValidationError("角色模板不存在")


def resolve_role_assignment(
    role_key: str,
    permissions: Optional[Iterable[str]],
    scope_type: str,
    venues: Optional[Iterable[str]],
) -> tuple[List[str], Dict[str, Any]]:
    template = ROLE_TEMPLATES_BY_KEY.get(role_key)
    if template is not None:
        return (
            list(template["permissions"]),
            normalize_scope(template["scope_type"], venues),
        )
    if permissions is None:
        raise AuthValidationError("自定义权限账号必须至少选择一项权限")
    return normalize_permissions(permissions), normalize_scope(scope_type, venues)


def hash_password(password: str) -> str:
    value = validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        value.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return "pbkdf2_sha256$%d$%s$%s" % (
        PASSWORD_ITERATIONS,
        _b64encode(salt),
        _b64encode(digest),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        if iterations < 100_000 or iterations > 2_000_000:
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256",
            str(password or "").encode("utf-8"),
            _b64decode(salt_text),
            iterations,
        )
        return hmac.compare_digest(candidate, _b64decode(digest_text))
    except (TypeError, ValueError, binascii.Error):
        return False


_DUMMY_PASSWORD_HASH = hash_password(_UNSAFE_PASSWORD_PLACEHOLDER)


def _permissions_from_json(value: str) -> List[str]:
    try:
        raw = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    selected = {item for item in raw if item in PERMISSION_KEYS}
    return [key for key in PERMISSION_KEYS if key in selected]


def normalize_scope(scope_type: str = SCOPE_ALL, venues: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """规范化账号数据范围；门店范围只保存去重后的显示名称。"""
    normalized_type = str(scope_type or SCOPE_ALL).strip().casefold()
    if normalized_type not in SCOPE_TYPES:
        raise AuthValidationError("数据范围必须是 all 或 venues")
    values = venues or []
    if isinstance(values, (str, bytes)):
        raise AuthValidationError("门店范围格式不正确")
    normalized_venues = []
    seen = set()
    for value in values:
        venue = str(value or "").strip()
        if not venue or venue in seen:
            continue
        seen.add(venue)
        normalized_venues.append(venue)
    if normalized_type == SCOPE_VENUES and not normalized_venues:
        raise AuthValidationError("门店范围至少选择一家门店")
    return {
        "scope_type": normalized_type,
        "venues": normalized_venues if normalized_type == SCOPE_VENUES else [],
    }


def _venues_from_json(value: str) -> List[str]:
    try:
        raw = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    if not raw or not any(str(item or "").strip() for item in raw):
        return []
    return normalize_scope(SCOPE_VENUES, raw)["venues"]


def effective_venue_scope(user: Dict[str, Any]) -> Optional[set[str]]:
    """返回账号实际可见门店；None 表示全量。管理员永远不受门店范围限制。"""
    permissions = set(user.get("permissions") or [])
    if "admin.access" in permissions or "users.manage" in permissions:
        return None
    if user.get("scope_type") != SCOPE_VENUES:
        return None
    return {str(value).strip() for value in user.get("venues") or [] if str(value).strip()}


def _public_user(row: sqlite3.Row) -> Dict[str, Any]:
    scope_type = str(row["scope_type"] or SCOPE_ALL).strip().casefold()
    if scope_type not in SCOPE_TYPES:
        scope_type = SCOPE_ALL
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "position": row["position"] or "",
        "role_key": str(row["role_key"] or ROLE_CUSTOM),
        "permissions": _permissions_from_json(row["permissions_json"]),
        "scope_type": scope_type,
        "venues": _venues_from_json(row["venue_scope_json"]),
        "is_active": bool(row["is_active"]),
        "last_login_at": row["last_login_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def user_count() -> int:
    conn = get_connection()
    try:
        row = conn.execute("SELECT COUNT(*) AS total FROM users").fetchone()
        return int(row["total"])
    finally:
        conn.close()


def bootstrap_admin(
    username: str,
    display_name: str,
    password: str,
) -> Dict[str, Any]:
    normalized_username = validate_username(username)
    normalized_name = validate_display_name(display_name)
    password_hash = hash_password(password)
    user_id = uuid.uuid4().hex
    permissions_json = json.dumps(SUPERADMIN_PERMISSIONS, ensure_ascii=False)
    now = _timestamp()

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            raise AuthConflictError("初始管理员已经创建")
        conn.execute(
            "INSERT INTO users (id, username, display_name, role_key, password_hash, "
            "permissions_json, scope_type, venue_scope_json, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (
                user_id,
                normalized_username,
                normalized_name,
                "super_admin",
                password_hash,
                permissions_json,
                SCOPE_ALL,
                "[]",
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        conn.commit()
        return _public_user(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def authenticate(username: str, password: str) -> Optional[Dict[str, Any]]:
    try:
        normalized_username = validate_username(username)
    except AuthValidationError:
        normalized_username = ""

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE username=? COLLATE NOCASE",
            (normalized_username,),
        ).fetchone()
        encoded = row["password_hash"] if row else _DUMMY_PASSWORD_HASH
        password_ok = verify_password(password, encoded)
        if row is None or not password_ok or not bool(row["is_active"]):
            return None
        now = _timestamp()
        conn.execute(
            "UPDATE users SET last_login_at=?, updated_at=updated_at WHERE id=?",
            (now, row["id"]),
        )
        conn.commit()
        refreshed = conn.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
        return _public_user(refreshed)
    finally:
        conn.close()


def create_session(user_id: str) -> Dict[str, str]:
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    expires_at = _now() + timedelta(seconds=SESSION_TTL_SECONDS)
    now = _timestamp()

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM auth_sessions WHERE expires_at<=?", (now,))
        conn.execute(
            "INSERT INTO auth_sessions "
            "(token_hash, user_id, csrf_token, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (_token_hash(token), user_id, csrf_token, _timestamp(expires_at), now),
        )
        conn.commit()
        return {
            "token": token,
            "csrf_token": csrf_token,
            "expires_at": _timestamp(expires_at),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_session(token: Optional[str]) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    hashed = _token_hash(token)
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT u.*, s.token_hash AS session_token_hash, "
            "s.csrf_token AS session_csrf_token, s.expires_at AS session_expires_at "
            "FROM auth_sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token_hash=?",
            (hashed,),
        ).fetchone()
        if row is None:
            return None
        try:
            expires_at = datetime.fromisoformat(row["session_expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            expires_at = _now() - timedelta(seconds=1)
        if expires_at <= _now() or not bool(row["is_active"]):
            conn.execute("DELETE FROM auth_sessions WHERE token_hash=?", (hashed,))
            conn.commit()
            return None
        return {
            "user": _public_user(row),
            "csrf_token": row["session_csrf_token"],
            "token_hash": row["session_token_hash"],
            "expires_at": row["session_expires_at"],
        }
    finally:
        conn.close()


def delete_session(token: Optional[str]) -> None:
    if not token:
        return
    conn = get_connection()
    try:
        conn.execute("DELETE FROM auth_sessions WHERE token_hash=?", (_token_hash(token),))
        conn.commit()
    finally:
        conn.close()


def list_users() -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY is_active DESC, created_at, username"
        ).fetchall()
        return [_public_user(row) for row in rows]
    finally:
        conn.close()


def create_user(
    username: str,
    display_name: str,
    password: str,
    permissions: Iterable[str],
    scope_type: str = SCOPE_ALL,
    venues: Optional[Iterable[str]] = None,
    position: str = "",
    role_key: str = ROLE_CUSTOM,
) -> Dict[str, Any]:
    normalized_username = validate_username(username)
    normalized_name = validate_display_name(display_name)
    normalized_position = validate_position(position)
    normalized_role_key = normalize_role_key(role_key)
    normalized_permissions, normalized_scope = resolve_role_assignment(
        normalized_role_key, permissions, scope_type, venues
    )
    password_hash = hash_password(password)
    user_id = uuid.uuid4().hex
    now = _timestamp()

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO users (id, username, display_name, position, role_key, password_hash, "
            "permissions_json, scope_type, venue_scope_json, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (
                user_id,
                normalized_username,
                normalized_name,
                normalized_position,
                normalized_role_key,
                password_hash,
                json.dumps(normalized_permissions, ensure_ascii=False),
                normalized_scope["scope_type"],
                json.dumps(normalized_scope["venues"], ensure_ascii=False),
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        conn.commit()
        return _public_user(row)
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise AuthConflictError("账号已存在") from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _is_user_manager(user: Dict[str, Any]) -> bool:
    permissions = set(user["permissions"])
    return bool(user["is_active"]) and {
        "admin.access",
        "users.manage",
    } <= permissions


def _ensure_another_user_manager(conn: sqlite3.Connection, user_id: str) -> None:
    rows = conn.execute(
        "SELECT * FROM users WHERE id<>? AND is_active=1",
        (user_id,),
    ).fetchall()
    if not any(_is_user_manager(_public_user(row)) for row in rows):
        raise AuthConflictError("必须保留至少一个有效的用户管理员")


def update_user(
    user_id: str,
    *,
    actor_user_id: str,
    display_name: Optional[str] = None,
    position: Optional[str] = None,
    permissions: Optional[Iterable[str]] = None,
    scope_type: Optional[str] = None,
    venues: Optional[Iterable[str]] = None,
    is_active: Optional[bool] = None,
    role_key: Optional[str] = None,
) -> Dict[str, Any]:
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise AuthNotFoundError("账号不存在")
        current = _public_user(row)
        next_name = (
            validate_display_name(display_name)
            if display_name is not None
            else current["display_name"]
        )
        next_position = (
            validate_position(position)
            if position is not None
            else current["position"]
        )
        if role_key is not None:
            next_role_key = normalize_role_key(role_key)
        elif permissions is not None or scope_type is not None or venues is not None:
            # Direct permission or scope edits intentionally become custom.
            # This also keeps older API clients compatible.
            next_role_key = ROLE_CUSTOM
        else:
            next_role_key = current["role_key"]
        requested_scope_type = scope_type if scope_type is not None else current["scope_type"]
        requested_venues = venues if venues is not None else current["venues"]
        next_permissions, next_scope = resolve_role_assignment(
            next_role_key,
            permissions if permissions is not None else current["permissions"],
            requested_scope_type,
            requested_venues,
        )
        next_active = bool(is_active) if is_active is not None else current["is_active"]

        if user_id == actor_user_id:
            if not next_active:
                raise AuthConflictError("不能停用当前登录账号")
            if not {"admin.access", "users.manage"} <= set(next_permissions):
                raise AuthConflictError("不能移除当前账号的后台或用户管理权限")

        next_user = dict(current)
        next_user.update(
            {
                "display_name": next_name,
                "position": next_position,
                "role_key": next_role_key,
                "permissions": next_permissions,
                "scope_type": next_scope["scope_type"],
                "venues": next_scope["venues"],
                "is_active": next_active,
            }
        )
        if _is_user_manager(current) and not _is_user_manager(next_user):
            _ensure_another_user_manager(conn, user_id)

        permissions_changed = next_permissions != current["permissions"]
        scope_changed = (
            next_scope["scope_type"] != current["scope_type"]
            or next_scope["venues"] != current["venues"]
        )
        active_changed = next_active != current["is_active"]
        conn.execute(
            "UPDATE users SET display_name=?, position=?, role_key=?, permissions_json=?, scope_type=?, "
            "venue_scope_json=?, is_active=?, updated_at=? WHERE id=?",
            (
                next_name,
                next_position,
                next_role_key,
                json.dumps(next_permissions, ensure_ascii=False),
                next_scope["scope_type"],
                json.dumps(next_scope["venues"], ensure_ascii=False),
                int(next_active),
                _timestamp(),
                user_id,
            ),
        )
        if permissions_changed or scope_changed or active_changed:
            conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
        refreshed = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        conn.commit()
        return _public_user(refreshed)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_user(user_id: str, *, actor_user_id: str) -> Dict[str, Any]:
    """永久删除内部账号，并撤销其所有登录会话。"""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise AuthNotFoundError("账号不存在")
        current = _public_user(row)
        if user_id == actor_user_id:
            raise AuthConflictError("不能删除当前登录账号")
        if _is_user_manager(current):
            _ensure_another_user_manager(conn, user_id)
        conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.commit()
        return current
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reset_password(user_id: str, password: str) -> Dict[str, Any]:
    password_hash = hash_password(password)
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise AuthNotFoundError("账号不存在")
        conn.execute(
            "UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
            (password_hash, _timestamp(), user_id),
        )
        conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
        refreshed = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        conn.commit()
        return _public_user(refreshed)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def has_permission(user: Dict[str, Any], permission: str) -> bool:
    permissions = set(user.get("permissions") or [])
    if permission != "users.manage" and "admin.access" in permissions:
        return True
    return permission in permissions
