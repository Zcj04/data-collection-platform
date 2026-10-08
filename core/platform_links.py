# -*- coding: utf-8 -*-
"""面向人工核查的源平台入口白名单。"""

from collections.abc import Iterable, Mapping
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit


_LOCAL_PLATFORM_IDS = frozenset({"octopus", "payment"})
_DOCUMENT_PLATFORM_IDS = frozenset({"coin_exchange"})


def _safe_https_url(value: Any) -> Optional[str]:
    """只接受不携带身份或动态参数的 HTTPS 人工入口。"""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or any(character.isspace() for character in candidate):
        return None
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    return candidate


def build_platform_links(
    platforms: Iterable[Tuple[str, str]],
    configured_links: Any,
) -> List[Dict[str, Any]]:
    """按既有平台白名单返回最小、安全的人工入口目录。"""
    links: Mapping[str, Any] = (
        configured_links if isinstance(configured_links, Mapping) else {}
    )
    result: List[Dict[str, Any]] = []
    for platform_id, display_name in platforms:
        platform_id = str(platform_id)
        url = _safe_https_url(links.get(platform_id))
        if platform_id in _LOCAL_PLATFORM_IDS:
            kind = "local"
        elif platform_id in _DOCUMENT_PLATFORM_IDS and url:
            kind = "document"
        elif url:
            kind = "console"
        else:
            kind = "unavailable"
        result.append({
            "id": platform_id,
            "name": str(display_name),
            "url": url,
            "kind": kind,
        })
    return result
