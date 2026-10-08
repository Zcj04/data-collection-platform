# -*- coding: utf-8 -*-
"""源平台入口白名单测试；可直接用 Python 运行。"""

from core.config import get as config_get
from core.platform_links import build_platform_links


def test_current_catalog_has_all_platforms_without_internal_details():
    configured_platforms = config_get("platforms", {}) or {}
    platforms = [
        (platform_id, item.get("name") or platform_id)
        for platform_id, item in configured_platforms.items()
    ]
    catalog = build_platform_links(platforms, config_get("platform_links", {}))

    assert len(catalog) == 13
    assert len({item["id"] for item in catalog}) == 13
    assert sum(bool(item["url"]) for item in catalog) == 10
    assert next(item for item in catalog if item["id"] == "jingjian")["url"] is None
    assert next(item for item in catalog if item["id"] == "octopus")["kind"] == "local"
    assert next(item for item in catalog if item["id"] == "payment")["kind"] == "local"
    assert next(item for item in catalog if item["id"] == "coin_exchange")["kind"] == "document"

    serialized = repr(catalog).lower()
    for forbidden in ("password", "cookie", "token", "file_path", "mtgsig"):
        assert forbidden not in serialized


def test_unsafe_or_dynamic_urls_fail_closed():
    platforms = [
        ("http", "HTTP"),
        ("userinfo", "用户信息"),
        ("query", "动态查询"),
        ("fragment", "片段"),
        ("script", "脚本"),
        ("safe", "安全入口"),
    ]
    configured = {
        "http": "http://example.com/",
        "userinfo": "https://user:secret@example.com/",
        "query": "https://example.com/?token=secret",
        "fragment": "https://example.com/#secret",
        "script": "javascript:alert(1)",
        "safe": "https://example.com/console/",
    }
    catalog = build_platform_links(platforms, configured)
    by_id = {item["id"]: item for item in catalog}

    assert by_id["safe"]["url"] == "https://example.com/console/"
    assert all(by_id[platform_id]["url"] is None for platform_id, _ in platforms[:-1])


if __name__ == "__main__":
    test_current_catalog_has_all_platforms_without_internal_details()
    test_unsafe_or_dynamic_urls_fail_closed()
    print("源平台入口白名单测试：全部通过（2 项）")
