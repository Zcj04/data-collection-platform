# -*- coding: utf-8 -*-
"""前端结构契约：防止单页工作台的导航、提示层和响应式样式回退。"""

import re
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "dashboard.html"
PORTAL = ROOT / "templates" / "portal.html"
STYLES = ROOT / "static" / "css" / "workbench.css"
ICONS = ROOT / "static" / "vendor" / "tabler-icons" / "tabler-icons.min.css"
LOGIN = ROOT / "templates" / "login.html"

EXPECTED_SECTIONS = {
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


class WorkbenchParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.ids = []
        self.sections = []
        self.h1_counts = {}
        self.toast_inside_section = None
        self.mismatched_tags = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set(attrs.get("class", "").split())
        element_id = attrs.get("id")
        if element_id:
            self.ids.append(element_id)
        if "section" in classes:
            self.sections.append(element_id)
            self.h1_counts[element_id] = 0
        current_section = next(
            (item[1] for item in reversed(self.stack) if item[2]), None
        )
        if tag == "h1" and current_section:
            self.h1_counts[current_section] += 1
        if element_id == "toast-container":
            self.toast_inside_section = current_section
        if tag not in {"area", "base", "br", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}:
            self.stack.append((tag, element_id, "section" in classes))

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1][0] != tag:
            self.mismatched_tags.append((self.stack[-1][0], tag))
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return


def _parse_template():
    parser = WorkbenchParser()
    parser.feed(TEMPLATE.read_text(encoding="utf-8"))
    return parser


def test_all_sections_have_unique_titles_and_ids():
    parser = _parse_template()
    assert set(parser.sections) == EXPECTED_SECTIONS
    assert all(parser.h1_counts[section] == 1 for section in EXPECTED_SECTIONS)
    assert len(parser.ids) == len(set(parser.ids))
    assert not parser.mismatched_tags
    assert not parser.stack


def test_global_feedback_and_required_dom_hooks_are_preserved():
    parser = _parse_template()
    source = TEMPLATE.read_text(encoding="utf-8")
    assert parser.toast_inside_section is None
    assert 'aria-live="polite"' in source
    assert "经营日报" not in source
    assert "/api/report" not in source
    for element_id in (
        "main-content",
        "mobile-drawer",
        "mobile-menu-button",
        "startDate",
        "endDate",
        "auto-collection-card",
        "auto-collection-state",
        "auto-collection-target",
        "auto-collection-last",
        "auto-collection-next",
        "auto-collection-coverage",
        "auto-collection-updated",
        "auto-collection-incident",
        "auto-collection-incident-text",
        "auto-collection-incident-action",
        "dailyDateInput",
        "dailyVenueInput",
        "bsCacheMeta",
        "dailyCacheMeta",
        "dailyOperationsStatus",
        "dailyMainlandRanking",
        "dailyMainlandMeta",
        "dailyHongKongRanking",
        "dailyHongKongMeta",
        "dailyStoreDialog",
        "dailyStoreDialogBody",
        "summaryTable",
        "analystMessages",
        "credFormsIn",
        "pd-calendar",
        "user-create-form",
        "user-table-body",
        "sidebar-current-user",
        "mobile-current-user",
    ):
        assert element_id in parser.ids


def test_visual_system_has_mobile_and_reduced_motion_guards():
    source = TEMPLATE.read_text(encoding="utf-8")
    css = STYLES.read_text(encoding="utf-8")
    assert '/static/css/workbench.css' in source
    assert "@media (max-width: 768px)" in css
    assert ".sidebar" in css and "display: none !important" in css
    assert ".metric-grid" in css and "grid-template-columns" in css
    assert ".analyst-layout" in css and ".forecast-layout" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert ":focus-visible" in css


def test_mobile_keeps_auto_collection_and_danger_actions_distinct():
    source = TEMPLATE.read_text(encoding="utf-8")
    css = STYLES.read_text(encoding="utf-8")
    mobile_css = css[
        css.index("@media (max-width: 768px)") : css.index("@media (max-width: 480px)")
    ]

    assert "workspace-location" in source
    assert "openAutoCollectionPlan()" in source
    assert ".workspace-status .workspace-location" in mobile_css
    assert ".workspace-status,\n  .shortcut-hint" not in mobile_css
    assert ".sidebar #sidebar-platforms .platform-run" in css
    assert ".sidebar #sidebar-platforms button {" not in css
    assert re.search(r"\.platform-stop\s*\{[^}]*rgba\(220, 38, 38", css, re.DOTALL)


def test_dashboard_and_payment_errors_do_not_report_stale_success():
    source = TEMPLATE.read_text(encoding="utf-8")

    assert 'id="bsEmptyMessage"' in source
    handler = re.search(
        r"async function checkScreenData\(options\)\{(?P<body>.*?)\n\}",
        source,
        flags=re.DOTALL,
    )
    assert handler
    assert "showEmpty('读取数据失败，请稍后重试')" in handler.group("body")
    assert "loading.classList.add('hidden')" in handler.group("body")
    assert "const response=await fetch('/api/payment-data/date/'+encodeURIComponent(date),{method:'DELETE'}),payload=await responsePayload(response);" in source
    assert "if(!response.ok){pdSetMsg(responseError(payload,'删除货款数据失败，请稍后重试'),true);return;}" in source


def test_payment_accounting_separates_entry_and_exact_month_end_workflows():
    source = TEMPLATE.read_text(encoding="utf-8")
    css = STYLES.read_text(encoding="utf-8")
    parser = _parse_template()

    for element_id in (
        "payment-nav-toggle",
        "payment-nav-children",
        "mobile-payment-nav-toggle",
        "mobile-payment-nav-children",
        "pa-tab-daily",
        "pa-tab-summary",
        "pa-tab-entry",
        "pa-tab-month",
        "pa-panel-daily",
        "pa-panel-summary",
        "pa-panel-entry",
        "pa-panel-month",
        "pa-daily-status",
        "pa-entry-status",
        "pa-entry-search",
        "pa-entry-filter",
        "pa-entry-rows",
        "pa-month-input",
        "pa-month-end-label",
        "pa-month-status",
        "pa-month-kpis",
        "pa-month-rows",
        "pa-unmatched",
        "pa-summary-status",
        "pa-summary-kpis",
        "pa-summary-search",
        "pa-summary-filter",
        "pa-summary-rows",
        "pa-summary-months",
        "dashboard-payment-action",
        "dashboard-payment-status",
    ):
        assert element_id in parser.ids

    assert "进场货款独立登记" in source
    assert "每日导入" in source
    assert "paOpenDailyImport" in source
    assert "togglePaymentNav" in source
    assert "paOpenPaymentTab('month')" in source
    assert "paOpenPaymentTab('entry')" in source
    assert "paOpenPaymentTab('summary')" in source
    assert source.count('data-payment-tab="daily"') == 2
    assert source.count('data-payment-tab="summary"') == 2
    assert source.count('data-payment-tab="month"') == 2
    assert source.count('data-payment-tab="entry"') == 2
    assert 'aria-controls="payment-nav-children"' in source
    assert 'aria-controls="mobile-payment-nav-children"' in source
    assert "只读取该月最后一个自然日" in source
    assert "/api/admin/payment-accounting/entry" in source
    assert "/api/admin/payment-accounting/month/" in source
    assert "/api/admin/payment-accounting/summary" in source
    assert "paSaveEntry" in source
    assert "缺少不等于 0 元" in source
    assert ".payment-close-strip" in css
    assert ".payment-ledger-table" in css
    assert re.search(
        r"\.payment-ledger-table-wrap\s*\{[^}]*contain:\s*paint;[^}]*overflow:\s*auto;",
        css,
        flags=re.DOTALL,
    )
    assert ".payment-month-kpis" in css
    assert ".payment-summary-kpis" in css
    assert "1分=1.5元" in source
    assert ".sidebar-group-toggle" in css
    assert ".sidebar-subnav" in css
    assert "workbench.css?v=2026091102" in source


def test_payment_managers_get_a_restricted_management_workbench():
    source = TEMPLATE.read_text(encoding="utf-8")

    assert "data-admin-only" in source
    assert "data-portal-entry" in source
    assert "function hasManagementWorkspaceAccess()" in source
    assert "function isPaymentOnlyWorkspace()" in source
    assert "function applyWorkspaceNavigation()" in source
    assert "showSection('paymentdata',false);\n    paSetTab('daily');" in source
    assert "return'货款管理员'" in source


def test_user_management_uses_backend_role_templates_before_advanced_permissions():
    source = TEMPLATE.read_text(encoding="utf-8")

    assert 'id="user-role-template" data-role-template' in source
    assert 'id="user-role-preview"' in source
    assert "function normalizeRoleTemplates(value)" in source
    assert "function applyRoleTemplate(" in source
    assert "function syncRoleTemplateSelection(" in source
    assert "role_key:document.getElementById('user-role-template').value" in source
    assert "role_key:roleKey" in source
    assert "高级权限" in source


def test_login_keeps_visible_focus_and_password_hint_relation():
    source = LOGIN.read_text(encoding="utf-8")

    assert 'aria-describedby="username-hint"' in source
    assert 'aria-describedby="password-hint"' in source
    assert 'id="username-hint"' in source
    assert ":focus-visible" in source
    assert "--brand2" not in source


def test_monitor_top10_separates_venues_visually():
    source = TEMPLATE.read_text(encoding="utf-8")
    assert "monitor-venue-a" in source
    assert "monitor-venue-b" in source
    assert "monitor-venue-start" in source


def test_daily_operations_prioritizes_region_rankings_and_store_drilldown():
    admin = TEMPLATE.read_text(encoding="utf-8")
    portal = PORTAL.read_text(encoding="utf-8")
    css = STYLES.read_text(encoding="utf-8")

    for source in (admin, portal):
        for label in (
            "有收入门店",
            "店均收入",
            "在营门店排行榜",
            "门店收入构成",
            "平台明细 · 按金额绝对值排序",
        ):
            assert label in source
        assert "daily-kpi-grid" in source
        assert "daily-region-grid" in source
        assert "daily-store-row" in source
        assert "daily-detail-dialog" in source
        assert "daily-donut" in source
        assert "dailyBarWidth" in source
        assert "dailyStoreRegion" in source
        assert "openDailyStoreDetail" in source
        assert "DAILY_PLATFORM_COLORS" in source
        assert "dailyPlatformColor" in source
        assert "daily-store-segment" in source
        assert "title" in source and "dailyMoney" in source
        assert "showModal()" in source
        assert "门店收入 TOP 5" not in source
        assert "全部门店排行" not in source
        assert 'class="daily-region-grid is-single"' in source
        assert re.search(
            r'aria-labelledby="daily(?:HongKongTitle|-hongkong-title)"[^>]*hidden[^>]*aria-hidden="true"',
            source,
        )

    assert ".daily-status-strip" in css
    assert ".daily-region-grid" in css
    assert ".daily-region-grid.is-single" in css
    assert ".daily-store-rail" in css
    assert ".daily-detail-dialog" in css
    assert ".daily-platform-row" in css
    assert ".daily-platform-legend" in css
    assert ".daily-store-segment" in css
    assert re.search(
        r"@media \(max-width: 768px\).*?\.daily-region-grid\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\)",
        css,
        flags=re.DOTALL,
    )


def test_portal_content_is_grouped_into_switchable_business_categories():
    source = PORTAL.read_text(encoding="utf-8")
    parser = WorkbenchParser()
    parser.feed(source)

    assert 'class="portal-category-nav"' in source
    assert 'role="tablist"' in source
    for category in ("overview", "daily", "summary", "forecast", "monitoring", "payment"):
        assert f'id="portal-category-{category}"' in source
        assert f'data-portal-category="{category}"' in source
        assert f'id="portal-panel-{category}"' in source
        assert f'aria-controls="portal-panel-{category}"' in source
    assert source.count('role="tabpanel"') == 6
    assert 'data-role-permission="monitoring.view"' in source
    assert 'data-role-permission="payment.view"' in source
    assert 'id="boss-report-download"' in source
    assert "activatePortalCategory" in source
    assert "@media (max-width: 760px)" in source
    assert ".portal-category-nav" in source
    assert "overflow-x: auto" in source
    assert len(parser.ids) == len(set(parser.ids))
    assert not parser.mismatched_tags
    assert not parser.stack


def test_portal_summary_table_has_expanded_readable_rows():
    source = PORTAL.read_text(encoding="utf-8")

    assert "#portal-summary-table thead th" in source
    assert "#portal-summary-table tbody td" in source
    assert "position: sticky" in source
    assert "height: 54px" in source
    assert "padding: 16px 18px" in source
    assert "#portal-summary-table tbody tr.row-total" in source
    assert "#portal-summary-table { display: none; }" in source
    assert ".summary-mobile-list { display: grid; gap: 8px; }" in source


def test_portal_summary_table_supports_cell_row_column_highlight_and_compact_index():
    source = PORTAL.read_text(encoding="utf-8")

    assert "applySummaryHighlight" in source
    assert "initSummaryHighlight" in source
    assert 'table.addEventListener("click"' in source
    assert 'cell.classList.contains("hl-cell")' in source
    assert 'String(column) === "序号"' in source
    assert 'String(columns[index]) === "序号"' in source
    assert ".is-index" in source
    assert "width: 68px" in source
    assert "max-width: 68px" in source


def test_portal_category_navigation_has_desktop_rail_tablet_scroll_and_mobile_grid():
    source = PORTAL.read_text(encoding="utf-8")

    assert "grid-template-columns: 226px minmax(0, 1fr)" in source
    assert ".portal-category-nav" in source and "grid-column: 1" in source
    assert "grid-row: 3" in source
    assert ".portal-category-panel" in source and "grid-column: 2" in source
    assert ".portal-main { display: block; padding-top: 24px; }" in source
    assert "overflow-x: auto" in source
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in source


def test_portal_mobile_first_screen_prioritizes_status_and_key_metrics():
    source = PORTAL.read_text(encoding="utf-8")

    assert ".portal-hero-copy p { display: none; }" in source
    assert "grid-template-columns: auto minmax(0, 1fr) auto;" in source
    assert ".freshness-card .status-pill" in source
    assert ".mobile-action-note { display: none; }" in source
    assert "flex: 0 0 160px;" in source


def test_admin_has_portal_entry_next_to_auto_collection_plan():
    source = TEMPLATE.read_text(encoding="utf-8")

    assert source.count('href="/portal" class="sidebar-link" data-portal-entry') == 0
    assert source.count('href="/portal" class="btn btn-ghost btn-sm" data-portal-entry') == 1
    assert source.index("自动采集计划") < source.index('href="/portal" class="btn btn-ghost btn-sm" data-portal-entry')


def test_user_management_has_collapsible_permissions_venue_picker_and_groups():
    source = TEMPLATE.read_text(encoding="utf-8")
    assert 'class="permission-details"' in source
    assert 'id="user-venues" data-scope-venues data-venue-picker-input type="text"' in source
    assert "/api/admin/venues" in source
    assert "userGroupLabel" in source
    assert "user-group-header" in source
    assert 'id="user-position"' in source
    assert "职位 / 分组" in source
    assert "selectedVenueValues" in source
    assert "在营门店" in source
    assert "deleteUser" in source
    assert "此操作不能恢复" in source
    assert 'id="venue-lifecycle-rows"' in source
    assert "saveVenueLifecycle" in source
    assert "opened_on" in source and "closed_on" in source


def test_hong_kong_panels_follow_selected_date_data():
    admin = TEMPLATE.read_text(encoding="utf-8")
    portal = PORTAL.read_text(encoding="utf-8")
    for source in (admin, portal):
        assert "hongkongRows.length" in source
        assert "classList.toggle('is-single'" in source or 'classList.toggle("is-single"' in source
    assert "hkItems.length" in admin
    assert "hongkongRanking.length" in portal


def test_dashboard_shows_read_only_auto_collection_status():
    source = TEMPLATE.read_text(encoding="utf-8")

    assert "/api/portal/collection/status?date=" in source
    assert "renderAutoCollectionStatus" in source
    assert "data_updated_at" in source
    assert "oldest_data_updated_at" in source
    assert "sources_present" in source and "sources_expected" in source
    assert "running_count" in source and "failed_count" in source
    assert "schedule.next_run_at" in source
    assert "schedule.next_start_date" in source
    assert "schedule.last_run" in source
    assert "lastRun.start_date" in source
    for element_id in (
        "auto-collection-target",
        "auto-collection-state",
        "auto-collection-last",
        "auto-collection-next",
        "auto-collection-coverage",
        "auto-collection-updated",
    ):
        assert re.search(rf"getElementById\('{element_id}'\)", source)
    assert "target.textContent=" in source
    assert "coverage.textContent=" in source
    assert "updated.textContent=" in source


def test_auto_collection_incident_routes_admin_to_log_health_review_without_posting():
    source = TEMPLATE.read_text(encoding="utf-8")

    assert re.search(
        r'id="auto-collection-incident"[^>]+\bhidden\b',
        source,
    )
    assert "查看并补采" in source
    assert "补采缺失/失败平台" in source
    assert "['partial','failed','expired'].includes(lastStatus)" in source
    assert "lastRun.resolved===true" in source
    assert "detail.textContent=" in source
    assert "incident.hidden=false" in source
    assert "incident.hidden=true" in source

    handler = re.search(
        r"function openAutoCollectionIncident\(\)\{(?P<body>.*?)\n\}",
        source,
        flags=re.DOTALL,
    )
    assert handler, "缺少自动采集异常处理入口"
    body = handler.group("body")
    assert "dqDate.value=date" in body
    assert "startDate.value=date" in body
    assert "endDate.value=date" in body
    assert "showSection('logs')" in body
    assert "fetch(" not in body
    assert "method:'POST'" not in body
    assert "if(name==='logs'){initDqDate();setTimeout(()=>{refreshLogs();startLogPolling();},100);}" in source
    assert "严格与自然日前一天比较" in source


def test_collection_task_status_refreshes_independently_of_summary():
    source = TEMPLATE.read_text(encoding="utf-8")

    assert "async function refreshTaskStatus(opts)" in source
    assert "fetch('/api/tasks/'+encodeURIComponent(date),{signal})" in source
    assert "updateSidebar(list);updateStatus(list);updateCompactStatus(list);renderLogs(list);updateStats(list);" in source
    assert "async function refreshDashboardData(opts)" in source
    assert "fetch('/api/summary/'+d,{signal})" in source
    assert "await refreshTaskStatus(opts);await refreshDashboardData(opts);" in source
    assert "Promise.all([fetch('/api/tasks/" not in source
    assert "if(state&&state.justFinished)refreshDashboardData();" in source
    assert "async function logPollLoop()" in source
    assert "if(state&&(state.hasActive||state.justFinished))await refreshLogs();" in source


def test_service_bootstraps_a_usable_python_runtime_before_pandas():
    source = (ROOT / "app_fastapi.py").read_text(encoding="utf-8")

    bootstrap = source.index("from runtime_bootstrap import ensure_supported_runtime")
    pandas_import = source.index("import pandas as pd")
    assert bootstrap < pandas_import
    assert "if __name__ == \"__main__\":\n    ensure_supported_runtime()" in source
    assert (ROOT / "runtime_bootstrap.py").exists()


def test_bigscreen_refreshes_automatically_and_daily_keeps_manual_cache():
    source = TEMPLATE.read_text(encoding="utf-8")

    assert "const _bigscreenCache=new Map(),_dailyOperationsCache=new Map();" in source
    assert "if(name==='bigscreen'){initBsDate();startPolling();}" in source
    assert "checkScreenData({force:true}).finally" in source
    assert "setTimeout(pollLoop,30000)" in source
    assert "loadDailyOperations({preferCache:true})" in source
    assert 'onclick="checkScreenData({force:true,manual:true})"' in source
    assert 'onclick="loadDailyOperations({force:true})"' in source
    assert "if(!options.force&&cached){renderScreenCache(cached)" in source
    assert "if(!options.force&&cached){renderDailyOperations(cached.data)" in source
    assert "显示缓存 · " in source


def test_bigscreen_missing_date_routes_to_dashboard_and_starts_one_collection():
    source = TEMPLATE.read_text(encoding="utf-8")

    assert "采集这一天" in source
    assert "data-collect-date=\"'+esc(d.date)+'\"" in source
    assert "collectMissingDate(this.dataset.collectDate)" in source

    handler = re.search(
        r"function collectMissingDate\(date\)\{(?P<body>.*?)\n\}",
        source,
        flags=re.DOTALL,
    )
    assert handler, "缺少大屏缺失日期采集入口"
    body = handler.group("body")
    assert "startDate.value=value" in body
    assert "endDate.value=value" in body
    assert "dashboard_startDate" in body
    assert "dashboard_endDate" in body
    assert "showSection('dashboard')" in body
    assert body.count("collect();") == 1


def test_source_platform_links_are_global_contextual_and_safe():
    admin = TEMPLATE.read_text(encoding="utf-8")
    portal = PORTAL.read_text(encoding="utf-8")
    css = STYLES.read_text(encoding="utf-8")
    login = LOGIN.read_text(encoding="utf-8")

    for source in (admin, portal):
        for element_id in (
            "platform-links-dialog",
            "platform-links-dialog-title",
            "platform-links-dialog-description",
            "platform-links-summary",
            "platform-links-list",
            "platform-links-close",
        ):
            assert f'id="{element_id}"' in source
        assert "/api/portal/platform-links" in source
        assert "target" in source and "_blank" in source
        assert "noopener noreferrer" in source
        assert "no-referrer" in source
        assert "daily-platform-source" in source
        assert "未配置安全入口" in source

    assert admin.count('aria-controls="platform-links-dialog"') >= 2
    assert 'id="portal-platform-links-trigger"' in portal
    assert "platformLinkInlineHtml(item.platform)" in admin
    assert "platformLinkInlineElement(item.platform)" in portal
    assert "源平台入口" not in login
    assert ".source-platform-grid" in css
    assert ".source-platform-card" in css
    assert ".daily-platform-source" in css
    assert re.search(
        r"@media \(max-width: 768px\).*?\.source-platform-grid\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\)",
        css,
        flags=re.DOTALL,
    )


def test_all_literal_tabler_icons_exist_in_local_bundle():
    source = TEMPLATE.read_text(encoding="utf-8")
    icon_css = ICONS.read_text(encoding="utf-8")
    icon_names = set(re.findall(r"\bti ti-([a-z0-9-]+)", source))
    missing = sorted(name for name in icon_names if f".ti-{name}:before" not in icon_css)
    assert not missing, f"Tabler 图标不存在: {missing}"


if __name__ == "__main__":
    for test in (
        test_all_sections_have_unique_titles_and_ids,
        test_global_feedback_and_required_dom_hooks_are_preserved,
        test_visual_system_has_mobile_and_reduced_motion_guards,
        test_mobile_keeps_auto_collection_and_danger_actions_distinct,
        test_dashboard_and_payment_errors_do_not_report_stale_success,
        test_payment_accounting_separates_entry_and_exact_month_end_workflows,
        test_payment_managers_get_a_restricted_management_workbench,
        test_user_management_uses_backend_role_templates_before_advanced_permissions,
        test_login_keeps_visible_focus_and_password_hint_relation,
        test_monitor_top10_separates_venues_visually,
        test_daily_operations_prioritizes_region_rankings_and_store_drilldown,
        test_portal_content_is_grouped_into_switchable_business_categories,
        test_admin_has_portal_entry_for_desktop_and_mobile,
        test_user_management_has_collapsible_permissions_venue_picker_and_groups,
        test_hong_kong_panels_follow_selected_date_data,
        test_dashboard_shows_read_only_auto_collection_status,
        test_auto_collection_incident_routes_admin_to_log_health_review_without_posting,
        test_collection_task_status_refreshes_independently_of_summary,
        test_service_bootstraps_a_usable_python_runtime_before_pandas,
        test_bigscreen_and_daily_reuse_page_cache_until_manual_refresh,
        test_source_platform_links_are_global_contextual_and_safe,
        test_all_literal_tabler_icons_exist_in_local_bundle,
    ):
        test()
    print("UI 结构契约测试：全部通过（21 项）")
