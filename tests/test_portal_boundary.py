# -*- coding: utf-8 -*-
"""第一阶段双入口契约：页面分流，并为后续鉴权固定 API 边界。"""

import ast
import re
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app_fastapi.py"
ADMIN_TEMPLATE = ROOT / "templates" / "dashboard.html"
PORTAL_TEMPLATE = ROOT / "templates" / "portal.html"

EXPECTED_ADMIN_SECTIONS = {
    "sec-dashboard",
    "sec-bigscreen",
    "sec-daily",
    "sec-analyst",
    "sec-forecast",
    "sec-monitoring",
    "sec-logs",
    "sec-credentials",
    "sec-paymentdata",
    "sec-users",
}

EXPECTED_PORTAL_SECTIONS = {
    "overview",
    "bigscreen",
    "summary",
    "daily",
    "forecast",
}

# 这里表达“可由数据门户使用”的读取白名单；移动端采集动作单独由
# PORTAL_ACTION_ROUTES 声明并使用后台权限。
EXPECTED_PORTAL_API_ROUTES = {
    ("GET", "/api/platform-links"),
    ("GET", "/api/summary/{date}"),
    ("GET", "/api/download/{date}"),
    ("GET", "/api/boss-report/{date}"),
    ("GET", "/api/boss-report/check/{date}"),
    ("GET", "/api/payment-data/years"),
    ("GET", "/api/payment-data/month/{year}/{month}"),
    ("GET", "/api/payment-data/date/{date}"),
    ("GET", "/api/payment-accounting/entry"),
    ("GET", "/api/payment-accounting/month/{year}/{month}"),
    ("GET", "/api/payment-accounting/summary"),
    ("GET", "/api/payment-accounting/outbound/months"),
    ("GET", "/api/payment-accounting/outbound/overview"),
    ("GET", "/api/payment-accounting/outbound/details"),
    ("GET", "/api/payment-accounting/outbound/status"),
    ("GET", "/api/monitoring/overview"),
    ("GET", "/api/dashboard/status"),
    ("GET", "/api/dashboard/data"),
    ("GET", "/api/daily-operations"),
    ("GET", "/api/collection/status"),
    ("GET", "/api/analyst/forecast"),
    ("GET", "/api/analyst/forecast/anomaly"),
}

EXPECTED_AUTH_AND_USER_API_ROUTES = {
    ("POST", "/api/auth/setup"),
    ("POST", "/api/auth/login"),
    ("GET", "/api/auth/me"),
    ("POST", "/api/auth/logout"),
    ("GET", "/api/admin/users"),
    ("POST", "/api/admin/users"),
    ("PATCH", "/api/admin/users/{user_id}"),
    ("POST", "/api/admin/users/{user_id}/reset-password"),
}

FORBIDDEN_PORTAL_API_PREFIXES = (
    "/api/tasks",
    "/api/collect",
    "/api/quality",
    "/api/logs",
    "/api/credentials",
    "/api/payment-data/upload",
    "/api/payment-accounting/outbound/collect",
    "/api/monitoring/sync",
    "/api/analyst/ask",
    "/api/analyst/config",
    "/api/analyst/sessions",
    "/api/analyst/session/",
)

FORBIDDEN_PORTAL_CONTROLS = (
    "全部采集",
    "单独采集",
    "补采缺失平台",
    "同步储值数据",
    "凭证管理",
    "采集日志",
    "上传 Excel",
    "保存凭证",
)


class SectionParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.section_ids = []
        self.ids = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        element_id = attrs.get("id")
        if element_id:
            self.ids.append(element_id)
        if "section" in set(attrs.get("class", "").split()):
            self.section_ids.append(element_id)


def _source_and_tree():
    source = APP.read_text(encoding="utf-8")
    return source, ast.parse(source)


def _app_routes(tree):
    """Return {(method, path): function node} without importing the app."""
    routes = {}
    method_names = {
        "get": "GET",
        "post": "POST",
        "put": "PUT",
        "patch": "PATCH",
        "delete": "DELETE",
    }
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not decorator.args:
                continue
            target = decorator.func
            if not (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "app"
                and target.attr in method_names
            ):
                continue
            try:
                path = ast.literal_eval(decorator.args[0])
            except (ValueError, TypeError):
                continue
            routes[(method_names[target.attr], path)] = node
    return routes


def _named_literal_collection(tree, name):
    value_node = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                value_node = node.value
                break
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                value_node = node.value
                break

    assert value_node is not None, "%s 必须在 app_fastapi.py 中显式定义" % name
    if (
        isinstance(value_node, ast.Call)
        and isinstance(value_node.func, ast.Name)
        and value_node.func.id == "frozenset"
        and len(value_node.args) == 1
    ):
        value_node = value_node.args[0]

    try:
        raw = ast.literal_eval(value_node)
    except (ValueError, TypeError) as exc:
        raise AssertionError("%s 必须是静态 (HTTP方法, 路径) 集合" % name) from exc

    normalized = set()
    for item in raw:
        assert isinstance(item, (tuple, list)) and len(item) == 2, (
            "%s 的每项必须是 (HTTP方法, 路径)" % name
        )
        method, path = item
        assert isinstance(method, str) and isinstance(path, str)
        normalized.add((method.upper(), path))
    return normalized


def _parse_template(path):
    parser = SectionParser()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser


def test_root_and_portal_render_portal_while_admin_renders_dashboard():
    source, tree = _source_and_tree()
    routes = _app_routes(tree)

    assert '"portal.html"' in source and "_PORTAL_HTML_TEMPLATE" in source
    assert '"dashboard.html"' in source and "_ADMIN_HTML_TEMPLATE" in source

    for path in ("/", "/portal"):
        assert ("GET", path) in routes
        route_source = ast.get_source_segment(source, routes[("GET", path)]) or ""
        assert "_PORTAL_HTML_TEMPLATE" in route_source

    assert ("GET", "/admin") in routes
    admin_source = ast.get_source_segment(source, routes[("GET", "/admin")]) or ""
    assert "_ADMIN_HTML_TEMPLATE" in admin_source


def test_admin_keeps_all_workbench_sections():
    parser = _parse_template(ADMIN_TEMPLATE)
    assert set(parser.section_ids) == EXPECTED_ADMIN_SECTIONS
    assert len(parser.section_ids) == len(EXPECTED_ADMIN_SECTIONS)
    assert len(parser.ids) == len(set(parser.ids))


def test_portal_contains_only_the_read_facing_sections_and_hooks():
    parser = _parse_template(PORTAL_TEMPLATE)
    source = PORTAL_TEMPLATE.read_text(encoding="utf-8")

    assert EXPECTED_PORTAL_SECTIONS <= set(parser.ids)
    assert {
        "portal-date",
        "freshness-status",
        "freshness-updated",
        "freshness-coverage",
        "freshness-next-run",
        "portal-current-user",
        "portal-logout-button",
        "portal-admin-entry",
        "portal-platform-links-trigger",
        "platform-links-dialog",
        "platform-links-list",
        "summary-download",
        "summary-cutoff",
        "daily-mainland-ranking",
        "daily-hongkong-ranking",
        "daily-store-dialog",
        "daily-store-dialog-body",
        "mobile-action-deck",
        "mobile-collect-action",
        "mobile-summary-download",
        "mobile-boss-report-download",
    } <= set(parser.ids)
    assert len(parser.ids) == len(set(parser.ids))
    assert 'href="/admin"' in source
    assert re.search(r'<a[^>]+id="portal-admin-entry"[^>]+\bhidden\b', source)
    assert "hasPermission(\"admin.access\")" in source
    assert "hasPermission(\"portal.download\")" in source
    assert "获取本月数据" in source
    assert "重新读取" in source
    assert "经营日报" not in source
    assert "/api/portal/report" not in source
    assert "/api/portal/collection/status?date=" in source
    for field in (
        "data_updated_at",
        "oldest_data_updated_at",
        "sources_present",
        "sources_expected",
        "running_count",
        "failed_count",
        "next_run_at",
        "next_start_date",
        "next_target_date",
    ):
        assert field in source
    assert "portal-section-menu" in source
    assert 'aria-label="打开页面导航"' in source
    assert "summary:focus-visible" in source
    assert 'id="freshness-status" role="status"' in source
    assert 'id="daily-status" class="daily-status-strip" role="status"' in source
    assert "在营门店排行榜" in source
    assert re.search(
        r'aria-labelledby="daily-hongkong-title"[^>]*hidden[^>]*aria-hidden="true"',
        source,
    )
    assert "daily-store-row" in source
    assert "openDailyStoreDetail" in source


def test_admin_summary_exposes_actual_source_cutoff():
    source = ADMIN_TEMPLATE.read_text(encoding="utf-8")

    assert 'id="summaryCutoff"' in source
    assert "renderSummaryCutoff" in source
    assert "s.meta" in source


def test_portal_collection_action_is_admin_only_and_uses_safe_text_hooks():
    source = PORTAL_TEMPLATE.read_text(encoding="utf-8")

    assert "/api/portal/collect?start_date=" in source
    assert "/api/quality/collect-missing" not in source
    assert "schedule.next_target_date === date" in source
    assert "系统将在下次计划自动重试" not in source
    assert "请联系管理员在后台补采" in source
    assert 'text("freshness-updated"' in source
    assert 'text("freshness-coverage"' in source
    assert 'text("freshness-next-run"' in source
    assert re.search(
        r"fetchJson\(`/api/portal/collection/status\?date=\$\{encodedDate\}`",
        source,
    )


def test_portal_source_has_no_management_write_api_or_sensitive_controls():
    source = PORTAL_TEMPLATE.read_text(encoding="utf-8")
    lower_source = source.lower()

    unsafe_methods = re.findall(
        r"\bmethod\s*:\s*['\"](?:post|put|patch|delete)['\"]",
        source,
        flags=re.IGNORECASE,
    )
    assert len(unsafe_methods) == 2
    assert re.search(
        r'portalFetch\("/api/auth/logout"\s*,\s*\{\s*method:\s*"POST"',
        source,
    )
    assert re.search(
        r'portalFetch\(\s*`/api/portal/collect\?start_date=',
        source,
    )
    for prefix in FORBIDDEN_PORTAL_API_PREFIXES:
        assert prefix not in lower_source, "门户不应引用管理 API: %s" % prefix
    for label in FORBIDDEN_PORTAL_CONTROLS:
        assert label not in source, "门户不应出现管理控件: %s" % label
    referenced_api_paths = re.findall(r"/api/[a-z0-9_/${}?=&.:-]+", lower_source)
    assert referenced_api_paths
    assert all(
        path.startswith("/api/portal/")
        or path in {"/api/auth/me", "/api/auth/logout"}
        for path in referenced_api_paths
    )


def test_api_inventory_is_complete_disjoint_and_portal_actions_are_separate():
    source, tree = _source_and_tree()
    decorated_api_routes = {
        route for route in _app_routes(tree) if route[1].startswith("/api/")
    }
    portal_routes = _named_literal_collection(tree, "PORTAL_API_ROUTES")
    admin_routes = _named_literal_collection(tree, "ADMIN_API_ROUTES")
    admin_namespace_routes = {
        route for route in decorated_api_routes if route[1].startswith("/api/admin/")
    }
    catalog_routes = {
        route for route in decorated_api_routes
        if not route[1].startswith("/api/admin/")
    }

    assert portal_routes == EXPECTED_PORTAL_API_ROUTES
    assert ("GET", "/api/report/{date}") not in decorated_api_routes
    assert all(method == "GET" for method, _ in portal_routes & admin_routes)
    assert EXPECTED_AUTH_AND_USER_API_ROUTES <= decorated_api_routes
    assert admin_namespace_routes >= {
        ("GET", "/api/admin/venues"),
        ("PATCH", "/api/admin/venues/{venue}"),
    }
    auth_routes = {
        route for route in EXPECTED_AUTH_AND_USER_API_ROUTES
        if not route[1].startswith("/api/admin/")
    }
    assert (
        portal_routes | admin_routes | auth_routes == catalog_routes
    )
    assert all(method in {"GET", "HEAD", "OPTIONS"} for method, _ in portal_routes)
    assert all(
        not any(
            path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/")
            for prefix in FORBIDDEN_PORTAL_API_PREFIXES
        )
        for _, path in portal_routes
    )
    assert 'PORTAL_API_PREFIX = "/api/portal"' in source
    assert 'ADMIN_API_PREFIX = "/api/admin"' in source
    assert 'PORTAL_ACTION_ROUTES' in source
    assert 'PORTAL_API_ROUTES + PORTAL_ACTION_ROUTES' in source
    assert "_mount_api_catalog(PORTAL_API_PREFIX, PORTAL_API_ROUTES" in source
    assert "_mount_api_catalog(ADMIN_API_PREFIX, ADMIN_API_ROUTES" in source


if __name__ == "__main__":
    for test in (
        test_root_and_portal_render_portal_while_admin_renders_dashboard,
        test_admin_keeps_all_ten_workbench_sections,
        test_portal_contains_only_the_read_facing_sections_and_hooks,
        test_portal_collection_action_is_admin_only_and_uses_safe_text_hooks,
        test_portal_source_has_no_management_write_api_or_sensitive_controls,
        test_api_inventory_is_complete_disjoint_and_portal_actions_are_separate,
    ):
        test()
    print("双入口与 API 边界契约测试：全部通过（6 项）")
