# -*- coding: utf-8 -*-
"""平台店铺/收银机到场地的安全映射工具。"""

import logging
from typing import Any, Dict, Iterable, Set


logger = logging.getLogger("mapping")


def add_unique_mapping(
    mapping: Dict[str, Any],
    ambiguous: Set[str],
    key: str,
    venue: Any,
    source: str,
) -> None:
    """写入映射；同一外部名称指向多个场地时移除并记录，禁止静默覆盖。"""
    key = str(key or "").strip()
    venue = str(venue or "").strip()
    if not key or not venue or key in ambiguous:
        return
    previous = mapping.get(key)
    if previous is None:
        mapping[key] = venue
        return
    if previous != venue:
        mapping.pop(key, None)
        ambiguous.add(key)
        logger.warning(
            "[%s] 映射冲突：%s 同时指向 %s 和 %s，已忽略该名称",
            source,
            key,
            previous,
            venue,
        )


def build_unique_mapping(rows: Iterable, source: str) -> Dict[str, str]:
    """从 ``(venue, names)`` 行构建不覆盖的换行分隔映射。"""
    mapping: Dict[str, str] = {}
    ambiguous: Set[str] = set()
    for venue, names in rows:
        for name in str(names or "").splitlines():
            add_unique_mapping(mapping, ambiguous, name, venue, source)
    return mapping
