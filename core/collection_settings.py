# -*- coding: utf-8 -*-
"""平台级批量采集开关。"""

from typing import Any, Dict, Iterable, List, Set, Tuple

from core.db import get_connection


def list_settings(platforms: Iterable[Tuple[str, str]]) -> List[Dict[str, Any]]:
    """返回平台开关；尚未保存的平台按默认开启处理。"""
    catalog = [(str(platform_id), str(name)) for platform_id, name in platforms]
    if not catalog:
        return []
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT platform, enabled, updated_at FROM platform_collection_settings"
        ).fetchall()
    finally:
        conn.close()
    saved = {str(row["platform"]): row for row in rows}
    return [
        {
            "id": platform_id,
            "name": name,
            "enabled": bool(saved[platform_id]["enabled"]) if platform_id in saved else True,
            "updated_at": saved[platform_id]["updated_at"] if platform_id in saved else None,
        }
        for platform_id, name in catalog
    ]


def enabled_platform_ids(platforms: Iterable[Tuple[str, str]]) -> Set[str]:
    return {
        item["id"]
        for item in list_settings(platforms)
        if item["enabled"]
    }


def set_enabled(platform: str, enabled: bool, valid_platforms: Set[str]) -> Dict[str, Any]:
    platform = str(platform or "").strip()
    if platform not in valid_platforms:
        raise ValueError("未知平台")
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO platform_collection_settings(platform, enabled, updated_at) "
            "VALUES(?,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT(platform) DO UPDATE SET enabled=excluded.enabled, "
            "updated_at=CURRENT_TIMESTAMP",
            (platform, 1 if enabled else 0),
        )
        conn.commit()
        row = conn.execute(
            "SELECT platform, enabled, updated_at FROM platform_collection_settings "
            "WHERE platform=?",
            (platform,),
        ).fetchone()
    finally:
        conn.close()
    return {
        "id": platform,
        "enabled": bool(row["enabled"]),
        "updated_at": row["updated_at"],
    }


def filter_enabled_adapters(adapters: Iterable[Any], platforms: Iterable[Tuple[str, str]]) -> List[Any]:
    enabled = enabled_platform_ids(platforms)
    return [adapter for adapter in adapters if adapter.platform_name in enabled]
