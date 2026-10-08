"""Offline browser layout check using current templates and synthetic API data."""
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "outputs" / "frontend-audit-20260915" / "responsive"
PERMISSIONS = ["portal.view", "portal.download", "boss_report.download", "monitoring.view",
               "payment.view", "payment.manage", "admin.access", "users.manage"]


def respond(route):
    path = urlsplit(route.request.url).path
    if path.startswith("/static/"):
        target = (ROOT / path.lstrip("/")).resolve()
        if target.is_relative_to(ROOT / "static") and target.is_file():
            route.fulfill(body=target.read_bytes(), content_type=mimetypes.guess_type(target)[0] or "application/octet-stream")
            return
    template = {"/login": "login", "/setup": "login", "/portal": "portal", "/store": "portal", "/admin": "dashboard"}.get(path)
    if template:
        route.fulfill(content_type="text/html", body=(ROOT / "templates" / f"{template}.html").read_text(encoding="utf-8").replace("{{ today }}", "2026-09-09").replace("{{ auth_mode }}", "setup" if path == "/setup" else "login"))
        return
    if path == "/api/auth/me":
        payload = {"user": {"id": "audit", "username": "audit.user", "display_name": "布局测试账号", "permissions": PERMISSIONS, "scope_type": "all"}, "csrf_token": "offline-fixture"}
    elif path == "/api/admin/users":
        payload = {"users": [{"id": "fixture-user", "username": "layout.test", "display_name": "测试门店负责人（较长名称）", "permissions": ["portal.view", "portal.download"], "scope_type": "venues", "venues": ["响应式测试门店（较长名称）"], "is_active": True}]}
    elif path == "/api/admin/venues":
        payload = {"items": [{"venue": "响应式测试门店（较长名称）", "opened_on": "2026-08-01", "closed_on": None}]}
    elif "/summary/" in path:
        payload = {"columns": ["序号", "场地", "负责人", "收入"], "rows": [[1, "响应式测试门店（较长名称）", "测试", 12345.67], [2, "第二家测试门店", "测试", None]]}
    else:
        route.fulfill(status=503, json={"detail": "离线布局检查：未连接业务接口"})
        return
    route.fulfill(json=payload)


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for width in (360, 390, 768, 920, 1366, 1920):
            context = browser.new_context(viewport={"width": width, "height": 900}, device_scale_factor=1)
            context.route("**/*", respond)
            page = context.new_page()
            for entry in ("login", "setup", "portal", "admin"):
                errors = []
                handler = lambda error: errors.append(error)
                page.on("pageerror", handler)
                page.goto(f"http://layout.test/{entry}")
                page.wait_for_timeout(300)
                sections = [entry] if entry in ("login", "setup") else (["overview", "daily", "summary", "forecast", "monitoring", "payment"] if entry == "portal" else ["dashboard", "bigscreen", "daily", "analyst", "forecast", "monitoring", "logs", "paymentdata", "users", "credentials"])
                for section in sections:
                    if entry == "portal":
                        page.locator(f"#portal-category-{section}").click()
                    elif entry == "admin":
                        page.evaluate("name => showSection(name)", section)
                    page.wait_for_timeout(100)
                    metrics = page.evaluate("""() => ({viewport:innerWidth, width:document.documentElement.scrollWidth,
                        overflow:[...document.querySelectorAll('body *')].filter(e=>{const r=e.getBoundingClientRect();return r.width && r.right>innerWidth+1 && getComputedStyle(e).position!=='fixed' && !e.closest('table')}).slice(0,8).map(e=>({tag:e.tagName,id:e.id,cls:e.className}))})""")
                    results.append({"entry": entry, "section": section, "screen_width": width, **metrics, "errors": [str(e) for e in errors]})
                    if width in (390, 1366) and section in ("login", "overview", "summary", "dashboard", "users"):
                        page.screenshot(path=str(OUTPUT / f"{entry}-{section}-{width}.png"), full_page=True)
                page.remove_listener("pageerror", handler)
            context.close()
        browser.close()
    (OUTPUT / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    failures = [r for r in results if r["width"] > r["viewport"] + 1 or r["errors"]]
    print(json.dumps({"cases": len(results), "failures": failures}, ensure_ascii=False))
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
