# -*- coding: utf-8 -*-
"""
企业内部数据平台 — FastAPI 入口
启动：python app_fastapi.py
数据门户：http://localhost:8010/portal
管理后台：http://localhost:8010/admin
"""

import asyncio
import hmac
import io
import ipaddress
import json
import logging
import os
import re
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from runtime_bootstrap import ensure_supported_runtime

if __name__ == "__main__":
    ensure_supported_runtime()

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from adapters.factory import build_adapters
from core.auto_collection import (
    collection_auto_scheduler,
    expire_stale_runs as expire_stale_collection_runs,
    get_collection_freshness,
)
from core.accounting_summary import accounting_venue_scope, load_period_summary_data
from core import backup as backup_service
from core.alerts import alert_dispatcher
from core.config import get as config_get
from core import credential_manager as credential_store
from core import collection_settings
from core.credential_manager import CredentialManager
from core.data_quality import inspect as inspect_data_quality
from core.dashboard_data import get_dashboard_data, get_dashboard_status
from core.daily_operations import get_daily_operations
from core.db import DB_PATH, get_connection, init_db
from core.logging import setup_logging
from core.maintenance import retention_scheduler
from core.health import readiness_status
from core import login_throttle
from core.monitoring import get_monitor_snapshot
from core.monitor_sync import monitor_auto_scheduler, monitor_sync_manager
from core.platform_links import build_platform_links
from core.process_lock import acquire_single_process_lock
from core.scheduler import Scheduler
from core.task_manager import TaskManager
from core import payment_store
from core import payment_upload
from core import payment_accounting
from core import outbound_payments
from core import venue_lifecycle
from core.outbound_collection import outbound_manager, credentials_configured as outbound_credentials_configured
from core import auth as auth_service
from core.analyst import llm as analyst_llm
from core.analyst.agent import (
    get_session_messages,
    list_sessions,
    run_question,
    run_question_stream,
)
from core.analyst.forecast import combined_forecast, detect_anomaly
from core.targets import (
    MAX_UPLOAD_BYTES,
    infer_month_from_filename,
    load_store_targets,
    parse_target_workbook,
    save_target_file,
    system_venue_names,
    target_source_name,
)
from crawlers import report_summary
from crawlers.generate_boss_report import build_data_dict, generate as generate_boss_report

# ---- 日志 ----
setup_logging(console=False)
logger = logging.getLogger("workbench")

# ---- 应用初始化 ----
@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    """统一管理后台线程，启动失败或关闭时按逆序释放。"""
    try:
        start_backup_scheduler()
        start_alert_dispatcher()
        start_monitor_auto_scheduler()
        start_collection_auto_scheduler()
        retention_scheduler.start()
        yield
    finally:
        retention_scheduler.stop()
        stop_collection_auto_scheduler()
        stop_monitor_auto_scheduler()
        stop_alert_dispatcher()
        stop_backup_scheduler()


app = FastAPI(title="企业内部数据平台", version="3.0.0", lifespan=app_lifespan)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 静态资源（Tailwind / Tabler 图标等本地化文件）
app.mount("/static", StaticFiles(directory=os.path.join(_BASE_DIR, "static")), name="static")

# 读取 HTML 模板
with open(os.path.join(_BASE_DIR, "templates", "dashboard.html"), "r", encoding="utf-8") as _f:
    _ADMIN_HTML_TEMPLATE = _f.read()
with open(os.path.join(_BASE_DIR, "templates", "portal.html"), "r", encoding="utf-8") as _f:
    _PORTAL_HTML_TEMPLATE = _f.read()
with open(os.path.join(_BASE_DIR, "templates", "login.html"), "r", encoding="utf-8") as _f:
    _LOGIN_HTML_TEMPLATE = _f.read()

# 同一 SQLite 只能由一个 Web 进程管理任务和自动排程。必须在任何启动写入前加锁。
acquire_single_process_lock(DB_PATH)

# 已有数据库在任何建表/迁移写入前，先与当前 Fernet 密钥成对备份。
_database_existed_before_init = os.path.isfile(DB_PATH)
if _database_existed_before_init and bool(config_get("backup.enabled", True)):
    try:
        _startup_backup = backup_service.create_backup(
            DB_PATH,
            credential_store.KEY_FILE,
        )
        backup_service.prune_backups(
            retention_days=int(config_get("backup.retention_days", 30))
        )
        logger.info("启动前成对备份已完成：%s", _startup_backup.name)
    except Exception:
        logger.exception("启动前成对备份失败")
        if bool(config_get("backup.required_on_startup", True)):
            raise

# 新库先创建密钥再建表；已有库沿用刚刚备份过的原密钥。
cred_mgr = CredentialManager()
init_db()

if not _database_existed_before_init and bool(config_get("backup.enabled", True)):
    backup_service.ensure_daily_backup(
        DB_PATH,
        credential_store.KEY_FILE,
        retention_days=int(config_get("backup.retention_days", 30)),
    )

backup_scheduler = backup_service.BackupScheduler(
    DB_PATH,
    credential_store.KEY_FILE,
)


def _mark_stale_tasks_interrupted() -> None:
    """服务启动时清理上次异常退出遗留的运行中记录。"""
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE tasks SET status='stopped', finished_at=?, step='已停止', "
            "error_msg='服务重启前收到停止请求' WHERE status='stop_requested'",
            (datetime.now(),),
        )
        conn.execute(
            "UPDATE tasks SET status='failed', finished_at=?, "
            "error_msg='服务重启，任务中断' WHERE status IN ('running','timeout_requested')",
            (datetime.now(),),
        )
        conn.execute(
            "UPDATE monitor_sync_runs SET status='failed', finished_at=?, "
            "error_msg='服务重启，监控同步中断' WHERE status='running'",
            (datetime.now(),),
        )
        conn.commit()
    finally:
        conn.close()


_mark_stale_tasks_interrupted()
outbound_payments.interrupt_stale_runs()
expire_stale_collection_runs()


def start_backup_scheduler() -> None:
    backup_scheduler.start()


def stop_backup_scheduler() -> None:
    backup_scheduler.stop()


def start_alert_dispatcher() -> None:
    alert_dispatcher.start()


def stop_alert_dispatcher() -> None:
    alert_dispatcher.stop()


def start_monitor_auto_scheduler() -> None:
    if _auto_collection_disabled():
        logger.warning("已通过环境变量停用自动采集与会员监控排程")
        return
    monitor_auto_scheduler.start()


def stop_monitor_auto_scheduler() -> None:
    monitor_auto_scheduler.stop()


def start_collection_auto_scheduler() -> None:
    if _auto_collection_disabled():
        return
    collection_auto_scheduler.start()


def stop_collection_auto_scheduler() -> None:
    collection_auto_scheduler.stop()


def _auto_collection_disabled() -> bool:
    """服务启动时的安全开关；手动采集接口仍保留，便于网络恢复后按需操作。"""
    return os.environ.get("WORKBUDDY_DISABLE_AUTO_COLLECTION", "").strip().lower() in {
        "1", "true", "yes", "on"
    }

PLATFORM_LIST: List[tuple] = [
    ("meituan", "美团"), ("yuntai", "芸苔"), ("leyaoyao", "乐摇摇"),
    ("duojinbao", "多金宝"), ("jingjian", "鲸舰"), ("starthing", "StarThing"),
    ("new_system", "新系统"), ("huilian", "汇联"), ("kpay", "KPay"),
    ("octopus", "八达通"), ("payment", "货款"), ("douyin", "抖音"),
    ("coin_exchange", "兑币机"), ("youcaihua", "油菜花"),
]

SCHEDULER_TIMEOUT: int = config_get("scheduler.single_task_timeout", 600)
SCHEDULER_WORKERS: int = config_get("scheduler.max_workers", 12)
SCHEDULER_PROCESS_ISOLATION: bool = bool(config_get("scheduler.process_isolation", True))


# 第一阶段明确现有 API 的使用边界并保留旧路径兼容，同时挂载两个新的
# API 命名空间。新旧路径统一执行身份、模块权限和数据范围校验。
PORTAL_API_PREFIX = "/api/portal"
ADMIN_API_PREFIX = "/api/admin"

# 门户清单只允许无管理副作用的读取请求。
PORTAL_API_ROUTES: Tuple[Tuple[str, str], ...] = (
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
    ("GET", "/api/collection/status"),
    ("GET", "/api/dashboard/status"),
    ("GET", "/api/dashboard/data"),
    ("GET", "/api/daily-operations"),
    ("GET", "/api/analyst/forecast"),
    ("GET", "/api/analyst/forecast/anomaly"),
)

# 手机门户允许管理员发起一批“本月 1 日至所选日期”的采集；
# 这条写操作单独列出，避免把门户的读接口误当成可写接口。
PORTAL_ACTION_ROUTES: Tuple[Tuple[str, str], ...] = (
    ("POST", "/api/collect"),
)

ADMIN_API_ROUTES: Tuple[Tuple[str, str], ...] = (
    ("GET", "/api/payment-data/history/{date}"),
    ("GET", "/api/tasks/{date}"),
    ("POST", "/api/collect"),
    ("POST", "/api/collect/single"),
    ("POST", "/api/tasks/stop/{task_id}"),
    ("GET", "/api/boss-report/{date}"),
    ("GET", "/api/boss-report/check/{date}"),
    ("GET", "/api/payment-data/years"),
    ("GET", "/api/payment-data/month/{year}/{month}"),
    ("GET", "/api/payment-data/date/{date}"),
    ("POST", "/api/payment-data/upload"),
    ("DELETE", "/api/payment-data/date/{date}"),
    ("GET", "/api/payment-accounting/entry"),
    ("POST", "/api/payment-accounting/entry/{venue}"),
    ("GET", "/api/payment-accounting/month/{year}/{month}"),
    ("GET", "/api/payment-accounting/summary"),
    ("GET", "/api/payment-accounting/outbound/months"),
    ("GET", "/api/payment-accounting/outbound/overview"),
    ("GET", "/api/payment-accounting/outbound/details"),
    ("GET", "/api/payment-accounting/outbound/status"),
    ("POST", "/api/payment-accounting/outbound/collect"),
    ("POST", "/api/payment-accounting/outbound/runs/{run_id}/stop"),
    ("POST", "/api/payment-accounting/outbound/runs/{run_id}/retry"),
    ("GET", "/api/monitoring/overview"),
    ("GET", "/api/monitoring/sync/status"),
    ("POST", "/api/monitoring/sync"),
    ("GET", "/api/quality/{date}"),
    ("POST", "/api/quality/collect-missing"),
    ("GET", "/api/logs/{date}"),
    ("GET", "/api/credentials"),
    ("POST", "/api/credentials/{platform}"),
    ("GET", "/api/credentials/{platform}"),
    ("GET", "/api/collection/platforms"),
    ("POST", "/api/collection/platforms/{platform}"),
    ("POST", "/api/analyst/ask"),
    ("POST", "/api/analyst/ask/stream"),
    ("GET", "/api/analyst/config"),
    ("GET", "/api/analyst/sessions"),
    ("GET", "/api/analyst/session/{session_id}"),
    ("GET", "/api/analyst/targets"),
    ("POST", "/api/analyst/targets/import"),
    ("GET", "/api/charts/platform-share"),
)

# 管理命名空间中的只读业务模块允许授予专门权限；写操作仍需管理员或模块管理权限。
BOSS_REPORT_PERMISSION_ROUTES: Tuple[Tuple[str, str], ...] = (
    ("GET", "/api/boss-report/{date}"),
    ("GET", "/api/boss-report/check/{date}"),
)
PAYMENT_VIEW_PERMISSION_ROUTES: Tuple[Tuple[str, str], ...] = (
    ("GET", "/api/payment-data/history/{date}"),
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
)
PAYMENT_MANAGE_PERMISSION_ROUTES: Tuple[Tuple[str, str], ...] = (
    ("POST", "/api/payment-data/upload"),
    ("POST", "/api/payment-accounting/entry/{venue}"),
    ("POST", "/api/payment-accounting/outbound/collect"),
    ("POST", "/api/payment-accounting/outbound/runs/{run_id}/stop"),
    ("POST", "/api/payment-accounting/outbound/runs/{run_id}/retry"),
    ("DELETE", "/api/payment-data/date/{date}"),
)
MONITORING_VIEW_PERMISSION_ROUTES: Tuple[Tuple[str, str], ...] = (
    ("GET", "/api/monitoring/overview"),
    ("GET", "/api/monitoring/sync/status"),
    ("GET", "/api/quality/{date}"),
)

_PUBLIC_AUTH_PATHS = {
    "/api/auth/login",
    "/api/auth/setup",
}
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class LoginPayload(BaseModel):
    username: str
    password: str


class SetupPayload(LoginPayload):
    display_name: str


class UserCreatePayload(SetupPayload):
    permissions: Optional[List[str]] = None
    position: str = ""
    scope_type: str = auth_service.SCOPE_ALL
    venues: Optional[List[str]] = None
    role_key: str = auth_service.ROLE_CUSTOM


class UserUpdatePayload(BaseModel):
    display_name: Optional[str] = None
    position: Optional[str] = None
    permissions: Optional[List[str]] = None
    scope_type: Optional[str] = None
    venues: Optional[List[str]] = None
    is_active: Optional[bool] = None
    role_key: Optional[str] = None


class CollectionPlatformPayload(BaseModel):
    enabled: bool


class PasswordResetPayload(BaseModel):
    password: str


class VenueLifecyclePayload(BaseModel):
    opened_on: Optional[str] = None
    closed_on: Optional[str] = None


class EntryPaymentPayload(BaseModel):
    entry_date: str
    amount: float
    note: str = ""


def _catalog_path_matches(template: str, path: str) -> bool:
    parts = []
    for segment in template.strip("/").split("/"):
        parts.append(r"[^/]+" if segment.startswith("{") and segment.endswith("}") else re.escape(segment))
    return bool(re.fullmatch(r"/" + r"/".join(parts), path))


def _catalog_contains(
    catalog: Tuple[Tuple[str, str], ...],
    method: str,
    path: str,
) -> bool:
    comparable_method = "GET" if method == "HEAD" else method
    return any(
        route_method == comparable_method and _catalog_path_matches(template, path)
        for route_method, template in catalog
    )


def _catalog_contains_namespaced(
    catalog: Tuple[Tuple[str, str], ...],
    method: str,
    path: str,
) -> bool:
    """同时匹配兼容路径和 /api/portal、/api/admin 命名空间路径。"""
    if _catalog_contains(catalog, method, path):
        return True
    for prefix in (PORTAL_API_PREFIX, ADMIN_API_PREFIX):
        if path.startswith(prefix + "/"):
            legacy_path = "/api" + path[len(prefix):]
            if _catalog_contains(catalog, method, legacy_path):
                return True
    return False


def _required_permission(method: str, path: str) -> Optional[str]:
    """对 canonical、legacy 和未知 API 统一 fail-closed 分类。"""
    if path in {"/", "/portal", "/store"}:
        return "portal.view"
    if path == "/admin" or path in {"/docs", "/redoc", "/openapi.json"}:
        return "admin.access"
    if path.startswith("/api/auth/"):
        return None
    if path.startswith("/api/admin/users"):
        return "users.manage"
    if method in _UNSAFE_METHODS and path.startswith("/api/admin/venues/"):
        return "users.manage"
    if _catalog_contains_namespaced(BOSS_REPORT_PERMISSION_ROUTES, method, path):
        return "boss_report.download"
    if _catalog_contains_namespaced(PAYMENT_MANAGE_PERMISSION_ROUTES, method, path):
        return "payment.manage"
    if _catalog_contains_namespaced(PAYMENT_VIEW_PERMISSION_ROUTES, method, path):
        return "payment.view"
    if _catalog_contains_namespaced(MONITORING_VIEW_PERMISSION_ROUTES, method, path):
        return "monitoring.view"
    if _catalog_contains_namespaced(PORTAL_ACTION_ROUTES, method, path):
        return "admin.access"
    if path.startswith("/api/portal/download/"):
        return "portal.download"
    if path.startswith("/api/portal/"):
        return "portal.view"
    if path.startswith("/api/admin/"):
        return "admin.access"
    if _catalog_contains(PORTAL_API_ROUTES, method, path):
        return "portal.download" if path.startswith("/api/download/") else "portal.view"
    if _catalog_contains(ADMIN_API_ROUTES, method, path):
        return "admin.access"
    if path.startswith("/api/"):
        return "admin.access"
    return None


def _same_origin_header_is_valid(request: Request) -> bool:
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return True
    expected = f"{request.url.scheme}://{request.headers.get('host', '')}".rstrip("/")
    return source.rstrip("/").startswith(expected + "/") or source.rstrip("/") == expected


def _api_error(detail: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        {"detail": detail},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _page_forbidden() -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><title>无权访问</title>"
        "<main style='font-family:sans-serif;max-width:560px;margin:12vh auto;padding:24px'>"
        "<h1>无权访问此页面</h1><p>当前账号没有所需权限，请联系管理员调整。</p>"
        "<p><a href='/portal'>返回数据门户</a></p></main>",
        status_code=403,
        headers={"Cache-Control": "no-store"},
    )


@app.middleware("http")
async def authentication_and_rbac(request: Request, call_next):
    path = request.url.path
    method = request.method.upper()

    if path.startswith("/static/") or path == "/favicon.ico" or path in {
        "/health/live", "/health/ready"
    }:
        return await call_next(request)

    setup_required = await run_in_threadpool(auth_service.user_count) == 0
    if setup_required:
        if path == "/setup" or path == "/api/auth/setup":
            if method in _UNSAFE_METHODS and not _same_origin_header_is_valid(request):
                return _api_error("安全校验失败，请从本机初始化页面操作", 403)
            return await call_next(request)
        if path.startswith("/api/"):
            return _api_error("请先创建初始管理员", 503)
        return RedirectResponse("/setup", status_code=303)

    if path == "/setup":
        return RedirectResponse("/login", status_code=303)
    if path in _PUBLIC_AUTH_PATHS or path == "/login":
        if method in _UNSAFE_METHODS and not _same_origin_header_is_valid(request):
            return _api_error("安全校验失败，请从登录页面操作", 403)
        return await call_next(request)

    session = await run_in_threadpool(
        auth_service.get_session,
        request.cookies.get(auth_service.SESSION_COOKIE_NAME)
    )
    if session is None:
        if path.startswith("/api/") or path == "/openapi.json":
            return _api_error("登录已失效，请重新登录", 401)
        return RedirectResponse("/login", status_code=303)

    request.state.user = session["user"]
    request.state.auth_session = session
    required_permission = _required_permission(method, path)
    has_workbench_access = (
        path == "/admin"
        and (
            auth_service.has_permission(session["user"], "admin.access")
            or auth_service.has_permission(session["user"], "payment.manage")
        )
    )
    if required_permission and not has_workbench_access and not auth_service.has_permission(
        session["user"], required_permission
    ):
        if path.startswith("/api/") or path == "/openapi.json":
            return _api_error("当前账号没有此操作权限", 403)
        return _page_forbidden()

    if method in _UNSAFE_METHODS and path.startswith("/api/"):
        supplied_csrf = request.headers.get("x-csrf-token", "")
        if (
            not supplied_csrf
            or not hmac.compare_digest(supplied_csrf, session["csrf_token"])
            or not _same_origin_header_is_valid(request)
        ):
            return _api_error("安全校验失败，请刷新页面后重试", 403)

    response = await call_next(request)
    if path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.middleware("http")
async def security_response_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=()",
    )
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "img-src 'self' data: https://fastapi.tiangolo.com; "
        "font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'",
    )
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    if request.url.scheme == "https":
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    return response


# ==================== 辅助函数 ====================


@app.get("/health/live", include_in_schema=False)
def health_live() -> JSONResponse:
    return JSONResponse(
        {"status": "alive"},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/health/ready", include_in_schema=False)
def health_ready() -> JSONResponse:
    result = readiness_status()
    return JSONResponse(
        result,
        status_code=200 if result["status"] == "ready" else 503,
        headers={"Cache-Control": "no-store"},
    )

def _load_summary_data(date: str) -> List[Dict[str, Any]]:
    """加载所选月截至目标日、各门店平台的最后一份累计数据。"""
    return load_period_summary_data(date)


def _venue_scope_for(request: Request) -> Optional[set[str]]:
    """读取当前账号的有效门店范围；管理员和全量账号返回 None。"""
    return auth_service.effective_venue_scope(request.state.user)


def _ensure_venue_allowed(scope: Optional[set[str]], venue: str) -> None:
    if scope is not None and str(venue or "").strip() and str(venue).strip() not in scope:
        raise HTTPException(status_code=403, detail="当前账号无权查看该门店")


def _validate_date_range(start_date: str, end_date: str) -> None:
    """校验 YYYY-MM-DD 格式及起止顺序，非法时抛 ValueError"""
    for value in (start_date, end_date):
        datetime.strptime(value, "%Y-%m-%d")
    if start_date > end_date:
        raise ValueError("start_date 不能晚于 end_date")


def _validate_monthly_collection_range(start_date: str, end_date: str) -> None:
    """采集结果按自然月累计，起始日必须是目标月第一天。"""
    _validate_date_range(start_date, end_date)
    if start_date[8:] != "01":
        raise ValueError("采集必须从目标月 1 日开始，并且不能跨月")
    if start_date[:7] != end_date[:7]:
        raise ValueError("采集不能跨月")


# ==================== 页面渲染 ====================

def _render_page(template: str) -> HTMLResponse:
    today = datetime.now().strftime("%Y-%m-%d")
    return HTMLResponse(
        template.replace("{{ today }}", today),
        headers={"Cache-Control": "no-store"},
    )


def _render_auth_page(mode: str) -> HTMLResponse:
    return HTMLResponse(
        _LOGIN_HTML_TEMPLATE.replace("{{ auth_mode }}", mode),
        headers={"Cache-Control": "no-store"},
    )


def _local_setup_request(request: Request) -> bool:
    # 本机代理的 TCP 地址不能证明请求来自本机操作人。
    if any(header in request.headers for header in (
        "forwarded", "x-forwarded-for", "x-real-ip", "x-forwarded-proto",
    )):
        return False
    host = request.client.host if request.client else ""
    if host == "testclient":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.casefold() == "localhost"


def _session_response(
    request: Request,
    user: Dict[str, Any],
    session: Dict[str, str],
    *,
    status_code: int = 200,
) -> JSONResponse:
    redirect = _default_destination(user)
    response = JSONResponse(
        {"user": user, "csrf_token": session["csrf_token"], "redirect": redirect},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )
    response.set_cookie(
        auth_service.SESSION_COOKIE_NAME,
        session["token"],
        max_age=auth_service.SESSION_TTL_SECONDS,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/",
    )
    return response


def _default_destination(user: Dict[str, Any]) -> str:
    """按账号能力选择登录后的工作入口。"""
    if auth_service.has_permission(user, "admin.access"):
        return "/admin"
    if auth_service.has_permission(user, "payment.manage"):
        return "/admin#paymentdata"
    if auth_service.effective_venue_scope(user) is not None:
        return "/store"
    return "/portal"


def _raise_auth_service_error(error: Exception) -> None:
    if isinstance(error, auth_service.AuthNotFoundError):
        raise HTTPException(status_code=404, detail=str(error)) from error
    if isinstance(error, auth_service.AuthConflictError):
        raise HTTPException(status_code=409, detail=str(error)) from error
    if isinstance(error, auth_service.AuthValidationError):
        raise HTTPException(status_code=422, detail=str(error)) from error
    raise error


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request) -> HTMLResponse:
    session = auth_service.get_session(
        request.cookies.get(auth_service.SESSION_COOKIE_NAME)
    )
    if session:
        destination = _default_destination(session["user"])
        return RedirectResponse(destination, status_code=303)
    return _render_auth_page("login")


@app.get("/setup", response_class=HTMLResponse, include_in_schema=False)
async def setup_page(request: Request) -> HTMLResponse:
    if not _local_setup_request(request):
        return _page_forbidden()
    return _render_auth_page("setup")


@app.post("/api/auth/setup", status_code=201)
def auth_setup(payload: SetupPayload, request: Request) -> JSONResponse:
    if not _local_setup_request(request):
        raise HTTPException(status_code=403, detail="初始管理员只能在本机创建")
    try:
        user = auth_service.bootstrap_admin(
            payload.username,
            payload.display_name,
            payload.password,
        )
    except Exception as error:
        _raise_auth_service_error(error)
    session = auth_service.create_session(user["id"])
    return _session_response(request, user, session, status_code=201)


@app.post("/api/auth/login")
async def auth_login(payload: LoginPayload, request: Request) -> JSONResponse:
    client_host = request.client.host if request.client else "unknown"
    wait_seconds = await run_in_threadpool(
        login_throttle.retry_after,
        client_host,
        payload.username,
    )
    if wait_seconds:
        return JSONResponse(
            {"detail": "登录尝试过于频繁，请稍后再试"},
            status_code=429,
            headers={"Cache-Control": "no-store", "Retry-After": str(wait_seconds)},
        )
    user = await run_in_threadpool(auth_service.authenticate, payload.username, payload.password)
    if user is None:
        wait_seconds = await run_in_threadpool(
            login_throttle.record_failure,
            client_host,
            payload.username,
        )
        await asyncio.sleep(0.2)
        if wait_seconds:
            return JSONResponse(
                {"detail": "登录尝试过于频繁，请稍后再试"},
                status_code=429,
                headers={"Cache-Control": "no-store", "Retry-After": str(wait_seconds)},
            )
        raise HTTPException(status_code=401, detail="账号或密码错误")
    await run_in_threadpool(login_throttle.clear, client_host, payload.username)
    session = await run_in_threadpool(auth_service.create_session, user["id"])
    return _session_response(request, user, session)


@app.get("/api/auth/me")
async def auth_me(request: Request) -> Dict[str, Any]:
    session = request.state.auth_session
    user = request.state.user
    return {
        "user": user,
        "permissions": user["permissions"],
        "permission_definitions": list(auth_service.PERMISSION_DEFINITIONS),
        "role_templates": list(auth_service.ROLE_TEMPLATES),
        "csrf_token": session["csrf_token"],
        "expires_at": session["expires_at"],
    }


@app.post("/api/auth/logout")
def auth_logout(request: Request) -> JSONResponse:
    auth_service.delete_session(
        request.cookies.get(auth_service.SESSION_COOKIE_NAME)
    )
    response = JSONResponse(
        {"message": "已安全退出"},
        headers={"Cache-Control": "no-store"},
    )
    response.delete_cookie(auth_service.SESSION_COOKIE_NAME, path="/")
    return response


@app.get("/api/admin/users")
def admin_list_users(request: Request) -> Dict[str, Any]:
    current_user_id = request.state.user["id"]
    users = auth_service.list_users()
    for user in users:
        user["is_self"] = user["id"] == current_user_id
        user["can_disable"] = not user["is_self"]
        user["can_delete"] = not user["is_self"]
        user["can_reset_password"] = not user["is_self"]
    return {
        "users": users,
        "permission_definitions": list(auth_service.PERMISSION_DEFINITIONS),
        "role_templates": list(auth_service.ROLE_TEMPLATES),
    }


def _admin_venue_items() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    roster = payment_accounting.get_venue_roster()
    by_venue: Dict[str, Dict[str, Any]] = {}
    for item in roster.get("rows", []):
        venue = str(item.get("venue") or "").strip()
        if not venue:
            continue
        operating = item.get("operating") is True
        previous = by_venue.get(venue)
        by_venue[venue] = {
            "venue": venue,
            "owner": str(item.get("owner") or "").strip(),
            "source_operating": operating or bool(
                previous and previous["source_operating"]
            ),
        }
    records = venue_lifecycle.list_lifecycle_records()
    for venue in venue_lifecycle.historical_venues() | set(records):
        by_venue.setdefault(venue, {
            "venue": venue,
            "owner": "",
            "source_operating": False,
        })
    today = datetime.now().strftime("%Y-%m-%d")
    for venue, item in by_venue.items():
        record = records.get(venue)
        fallback_operating = bool(
            item["source_operating"]
            and report_summary.is_current_operating_venue(venue)
        )
        item.update({
            "opened_on": record.get("opened_on") if record else None,
            "closed_on": record.get("closed_on") if record else None,
            "lifecycle_configured": bool(record),
            "operating": venue_lifecycle.is_operating_on(
                venue,
                today,
                record=record,
                fallback_operating=fallback_operating,
            ),
            "updated_at": record.get("updated_at") if record else None,
        })
    items = sorted(
        by_venue.values(),
        key=lambda item: (not item["operating"], item["venue"]),
    )
    return items, roster


@app.get("/api/admin/venues")
def admin_list_venues() -> Dict[str, Any]:
    """返回门店清单及可维护的营业日期边界。"""
    items, roster = _admin_venue_items()
    return {
        "venues": [item["venue"] for item in items],
        "items": items,
        "source": roster.get("source", "unknown"),
        "warning": roster.get("warning", ""),
    }


@app.patch("/api/admin/venues/{venue}")
def admin_update_venue_lifecycle(
    venue: str,
    payload: VenueLifecyclePayload,
    request: Request,
) -> Dict[str, Any]:
    venue_name = str(venue or "").strip()
    items, _ = _admin_venue_items()
    if venue_name not in {item["venue"] for item in items}:
        raise HTTPException(status_code=404, detail="门店不在标准或历史门店清单中")
    try:
        venue_lifecycle.save_lifecycle(
            venue_name,
            payload.opened_on,
            payload.closed_on,
            updated_by=str(request.state.user.get("id") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    saved_items, _ = _admin_venue_items()
    saved = next(item for item in saved_items if item["venue"] == venue_name)
    return {"message": "门店营业日期已保存", "item": saved}


@app.post("/api/admin/users", status_code=201)
def admin_create_user(payload: UserCreatePayload) -> Dict[str, Any]:
    try:
        user = auth_service.create_user(
            payload.username,
            payload.display_name,
            payload.password,
            payload.permissions,
            payload.scope_type,
            payload.venues,
            payload.position,
            payload.role_key,
        )
    except Exception as error:
        _raise_auth_service_error(error)
    return {"user": user, "message": "账号创建成功"}


@app.patch("/api/admin/users/{user_id}")
def admin_update_user(
    user_id: str,
    payload: UserUpdatePayload,
    request: Request,
) -> Dict[str, Any]:
    try:
        user = auth_service.update_user(
            user_id,
            actor_user_id=request.state.user["id"],
            display_name=payload.display_name,
            position=payload.position,
            permissions=payload.permissions,
            scope_type=payload.scope_type,
            venues=payload.venues,
            is_active=payload.is_active,
            role_key=payload.role_key,
        )
    except Exception as error:
        _raise_auth_service_error(error)
    return {"user": user, "message": "账号信息已更新"}


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: str, request: Request) -> Dict[str, Any]:
    try:
        user = auth_service.delete_user(
            user_id,
            actor_user_id=request.state.user["id"],
        )
    except Exception as error:
        _raise_auth_service_error(error)
    return {"user": user, "message": "账号已删除"}


@app.post("/api/admin/users/{user_id}/reset-password")
def admin_reset_user_password(
    user_id: str,
    payload: PasswordResetPayload,
    request: Request,
) -> Dict[str, Any]:
    if user_id == request.state.user["id"]:
        raise HTTPException(status_code=409, detail="不能在用户管理中重置当前登录账号的密码")
    try:
        user = auth_service.reset_password(user_id, payload.password)
    except Exception as error:
        _raise_auth_service_error(error)
    return {"user": user, "message": "密码已重置，该账号需要重新登录"}


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
@app.get("/portal", response_class=HTMLResponse, include_in_schema=False)
async def portal() -> HTMLResponse:
    """企业数据门户；只读查询由服务端按账号数据范围返回。"""
    return _render_page(_PORTAL_HTML_TEMPLATE)


@app.get("/store", response_class=HTMLResponse, include_in_schema=False)
async def store_portal() -> HTMLResponse:
    """门店负责人入口；与门户共用页面，但默认按账号绑定门店读取。"""
    return _render_page(_PORTAL_HTML_TEMPLATE)


@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_dashboard() -> HTMLResponse:
    """数据管理后台；需要管理员权限，用户管理可配置账号数据范围。"""
    return _render_page(_ADMIN_HTML_TEMPLATE)


# ==================== API: 源平台入口 ====================

@app.get("/api/platform-links")
async def platform_links() -> Dict[str, List[Dict[str, Any]]]:
    """返回人工追查入口；不下发爬虫 API、凭证或本地文件路径。"""
    return {
        "platforms": build_platform_links(
            PLATFORM_LIST,
            config_get("platform_links", {}),
        )
    }


@app.get("/api/collection/platforms")
def collection_platform_settings() -> Dict[str, Any]:
    settings = collection_settings.list_settings(PLATFORM_LIST)
    return {
        "platforms": settings,
        "enabled_count": sum(1 for item in settings if item["enabled"]),
        "total_count": len(settings),
    }


@app.post("/api/collection/platforms/{platform}")
def update_collection_platform(
    platform: str,
    payload: CollectionPlatformPayload,
) -> Dict[str, Any]:
    try:
        updated = collection_settings.set_enabled(
            platform,
            payload.enabled,
            {platform_id for platform_id, _name in PLATFORM_LIST},
        )
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=404)
    return {
        "status": "updated",
        "platform": updated,
        "message": (
            "已加入全部采集"
            if updated["enabled"]
            else "已从全部采集中排除"
        ),
    }


# ==================== API: 任务管理 ====================

@app.get("/api/tasks/{date}")
def get_tasks(date: str) -> List[Dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, platform, status, error_msg, created_at FROM tasks t1 "
            "WHERE date=? AND created_at = ("
            "  SELECT MAX(created_at) FROM tasks t2 "
            "  WHERE t2.platform=t1.platform AND t2.date=?"
            ") ORDER BY platform",
            (date, date),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.post("/api/collect")
def start_collect(
    start_date: str = Query(...),
    end_date: str = Query(...),
    request: Request = None,
) -> JSONResponse:
    if request is not None and _venue_scope_for(request) is not None:
        return JSONResponse(
            {"status": "error", "message": "限定门店账号不能发起全公司采集。"},
            status_code=403,
        )
    try:
        _validate_monthly_collection_range(start_date, end_date)
    except ValueError as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    task_mgr = TaskManager()
    scheduler = Scheduler(
        task_mgr=task_mgr,
        max_workers=SCHEDULER_WORKERS,
        single_task_timeout=SCHEDULER_TIMEOUT,
        process_isolation=SCHEDULER_PROCESS_ISOLATION,
    )
    settings = collection_settings.list_settings(PLATFORM_LIST)
    enabled_ids = {item["id"] for item in settings if item["enabled"]}
    adapters = [
        adapter for adapter in build_adapters()
        if adapter.platform_name in enabled_ids
    ]
    if not adapters:
        return JSONResponse(
            {"status": "error", "message": "没有开启可参与全部采集的平台，请先打开平台开关。"},
            status_code=400,
        )
    ok = scheduler.launch_background(start_date, end_date, adapters)
    if not ok:
        return JSONResponse({
            "status": "busy",
            "message": "上一批采集仍在进行中，请等待完成后再触发。",
        }, status_code=409)
    logger.info(f"采集启动: {start_date} ~ {end_date}")
    skipped = [item["name"] for item in settings if not item["enabled"]]
    return {
        "status": "started",
        "start": start_date,
        "end": end_date,
        "message": "已启动全部采集：%s 个平台%s" % (
            len(adapters),
            ("（已跳过：%s）" % "、".join(skipped)) if skipped else "",
        ),
        "platforms": [adapter.platform_name for adapter in adapters],
        "skipped_platforms": skipped,
    }


@app.post("/api/collect/single")
def start_single_collect(
    platform: str = Query(...),
    start_date: str = Query(...),
    end_date: str = Query(...),
) -> JSONResponse:
    try:
        _validate_monthly_collection_range(start_date, end_date)
    except ValueError as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    task_mgr = TaskManager()
    scheduler = Scheduler(
        task_mgr=task_mgr,
        max_workers=1,
        single_task_timeout=SCHEDULER_TIMEOUT,
        process_isolation=SCHEDULER_PROCESS_ISOLATION,
    )
    adapters = build_adapters()
    target = [a for a in adapters if a.platform_name == platform]
    if not target:
        return JSONResponse({"error": "unknown platform"}, status_code=404)
    ok = scheduler.launch_background(start_date, end_date, target)
    if not ok:
        return JSONResponse({"status": "busy", "message": "上一批采集仍在进行中"}, status_code=409)
    return {"status": "started", "platform": platform}


@app.post("/api/tasks/stop/{task_id}")
def stop_task(task_id: str) -> Dict:
    requested = TaskManager().request_stop(task_id)
    if not requested:
        return JSONResponse(
            {"status": "not_found_or_finished", "message": "任务不存在或已结束"},
            status_code=404,
        )
    return {"status": "stop_requested", "message": "已发送停止请求，等待当前平台调用释放"}


# ==================== API: 汇总报表 ====================

@app.get("/api/summary/{date}")
def get_summary(date: str, request: Request) -> Dict:
    scope = _venue_scope_for(request)
    data_list, summary_meta = load_period_summary_data(
        date,
        scope,
        include_metadata=True,
        platform_catalog=PLATFORM_LIST,
    )
    if not data_list:
        return {"columns": [], "rows": [], "meta": summary_meta}
    venue_scope = accounting_venue_scope(data_list)
    if not venue_scope:
        return {"columns": [], "rows": [], "meta": summary_meta}
    # 核算表只保留所选月份截至该日至少有一项非零数值的门店。
    result = report_summary.main(
        data_list,
        active_venues=venue_scope,
    )
    if result and len(result) > 1:
        return {"columns": result[0], "rows": result[1:], "meta": summary_meta}
    return {"columns": [], "rows": [], "meta": summary_meta}


@app.get("/api/download/{date}")
def download_summary(date: str, request: Request):
    scope = _venue_scope_for(request)
    data_list, summary_meta = load_period_summary_data(
        date,
        scope,
        include_metadata=True,
        platform_catalog=PLATFORM_LIST,
    )
    if not data_list:
        return JSONResponse({"error": "no data"}, status_code=404)

    venue_scope = accounting_venue_scope(data_list)
    if not venue_scope:
        return JSONResponse({"error": "no nonzero data"}, status_code=404)
    result = report_summary.main(
        data_list,
        active_venues=venue_scope,
    )
    if not result or len(result) < 2:
        return JSONResponse({"error": "summary failed"}, status_code=500)

    df = pd.DataFrame(result[1:], columns=result[0])
    status_df = pd.DataFrame([
        {
            "平台": item["name"],
            "平台标识": item["platform"],
            "状态": item["status_label"],
            "最新数据日": item["latest_date"] or "—",
            "最新任务状态": item["task_status"] or "—",
            "门店数": item["venue_count"],
        }
        for item in summary_meta.get("platforms", [])
    ])
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=date)
        status_df.to_excel(writer, index=False, sheet_name="数据状态")
    output.seek(0)
    from urllib.parse import quote

    filename = f"汇总报表_{date}.xlsx"
    ascii_name = filename.encode("ascii", "ignore").decode() or "summary.xlsx"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": (
                f"attachment; filename={ascii_name}; filename*=UTF-8''{quote(filename)}"
            )
        },
    )


# ==================== API: 老板报表（一键生成） ====================

@app.get("/api/boss-report/{date}")
def boss_report(date: str, request: Request):
    """
    一键生成老板"每月货款比"报表，返回文件供下载
    """
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return JSONResponse({"error": "日期格式错误，请使用 YYYY-MM-DD"}, status_code=400)

    scope = _venue_scope_for(request)
    fd, scoped_path = tempfile.mkstemp(prefix="workbuddy-boss-", suffix=".xlsx")
    os.close(fd)
    try:
        output_path = generate_boss_report(
            date,
            output_path=scoped_path,
            venue_scope=scope,
        )
        if not output_path or not os.path.exists(output_path):
            return JSONResponse({"error": "生成失败，目标日期无数据"}, status_code=404)

        # 读取文件并返回下载流
        with open(output_path, "rb") as f:
            output = io.BytesIO(f.read())
        output.seek(0)
        from urllib.parse import quote

        filename = f"每月货款比 {date}.xlsx"
        ascii_name = filename.encode("ascii", "ignore").decode() or "boss_report.xlsx"
        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": (
                    f"attachment; filename={ascii_name}; filename*=UTF-8''{quote(filename)}"
                )
            },
        )
    finally:
        if scoped_path:
            try:
                os.unlink(scoped_path)
            except FileNotFoundError:
                pass


@app.get("/api/boss-report/check/{date}")
def boss_report_check(date: str, request: Request) -> Dict:
    """检查指定日期数据是否存在"""
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return {"status": "error", "message": "日期格式错误，请使用 YYYY-MM-DD", "has_data": False}

    # 检查数据库是否有数据
    data_dict = build_data_dict(date, _venue_scope_for(request))
    if not data_dict:
        return {"status": "ok", "has_data": False, "message": f"日期 {date} 无数据，请先采集"}
    return {"status": "ok", "has_data": True, "venue_count": len(data_dict)}


# ==================== API: 货款数据（导入/查看/删除） ====================

@app.get("/api/payment-data/years")
def payment_data_years(request: Request) -> Dict:
    """有导入数据的年份列表"""
    return {"years": payment_store.list_available_years(_venue_scope_for(request))}


@app.get("/api/payment-data/month/{year}/{month}")
def payment_data_month(year: int, month: int, request: Request) -> Dict:
    """某年某月有货款数据的日期列表"""
    if month < 1 or month > 12:
        return JSONResponse({"error": "月份必须在 1-12 之间"}, status_code=400)
    dates = payment_store.list_dates(year, month, _venue_scope_for(request))
    return {"year": year, "month": month, "dates": dates}


@app.get("/api/payment-data/date/{date}")
def payment_data_date(date: str, request: Request) -> Dict:
    """某日的货款数据明细"""
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return JSONResponse({"error": "日期格式错误，请使用 YYYY-MM-DD"}, status_code=400)
    scope = _venue_scope_for(request)
    info = payment_store.get_date_info(date, scope)
    rows = payment_store.get_date_rows(date, scope)
    return {"info": info, "rows": rows}


@app.post("/api/payment-data/upload")
def payment_data_upload(
    date: str = Form(...),
    file: UploadFile = File(...),
    request: Request = None,
) -> JSONResponse:
    """上传某日的货款Excel（店名.1 / 求和项:金额），整日覆盖"""
    if _venue_scope_for(request) is not None:
        return JSONResponse({"error": "限定门店账号不能执行整日货款覆盖"}, status_code=403)
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return JSONResponse({"error": "日期格式错误，请使用 YYYY-MM-DD"}, status_code=400)

    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        return JSONResponse({"error": "请上传 .xlsx 格式的 Excel 文件"}, status_code=400)

    try:
        content = payment_upload.read_upload(file.file)
        df = pd.read_excel(io.BytesIO(content), nrows=payment_upload.MAX_ROWS + 1)
        if len(df) > payment_upload.MAX_ROWS:
            raise ValueError("货款明细不能超过 10000 行")
        from crawlers.payment_crawler import _clean_df
        tenant_list = _clean_df(df)
    except Exception as e:
        return JSONResponse(
            {"error": f"Excel解析失败：{e}。请确认文件含「店名」与「金额」类列（如 店名.1/求和项:金额 或 店名/金额）"},
            status_code=400,
        )

    if not tenant_list:
        return JSONResponse({"error": "Excel中没有有效的货款数据行"}, status_code=400)

    rows = [{"shop_name": t["货款店铺名"], "amount": t["基础货款"]} for t in tenant_list]
    try:
        count = payment_store.replace_date_rows(date, rows, source_file=file.filename,
                                               actor_user_id=request.state.user["id"])
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"保存失败：{e}"}, status_code=500)

    return {
        "status": "ok",
        "date": date,
        "count": count,
        "total": round(sum(r["amount"] for r in rows), 2),
        "message": f"已导入 {date} 的货款数据，共 {count} 行",
    }


@app.get("/api/payment-data/history/{date}")
def payment_data_history(date: str, request: Request) -> Dict:
    if _venue_scope_for(request) is not None:
        raise HTTPException(status_code=403, detail="限定门店账号不能查看整日货款修订记录")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式错误，请使用 YYYY-MM-DD")
    return {"date": date, "history": payment_store.list_history(date)}


@app.delete("/api/payment-data/date/{date}")
def payment_data_delete(date: str, request: Request) -> Dict:
    """删除某日的货款数据"""
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return JSONResponse({"error": "日期格式错误，请使用 YYYY-MM-DD"}, status_code=400)
    if _venue_scope_for(request) is not None:
        return JSONResponse(
            {"error": "限定门店账号不能执行整日货款删除，请使用管理员账号"},
            status_code=403,
        )
    count = payment_store.delete_date(date, actor_user_id=request.state.user["id"])
    return {"status": "ok", "date": date, "deleted": count}


@app.get("/api/payment-accounting/entry")
def payment_accounting_entry_list(request: Request) -> Dict:
    """标准门店清单与各门店最新进场货款。"""
    return payment_accounting.list_entry_payments(_venue_scope_for(request))


@app.post("/api/payment-accounting/entry/{venue}")
def payment_accounting_entry_save(
    venue: str,
    payload: EntryPaymentPayload,
    request: Request,
) -> JSONResponse:
    """为标准门店追加一版进场货款修订。"""
    try:
        _ensure_venue_allowed(_venue_scope_for(request), venue)
        saved = payment_accounting.save_entry_payment(
            venue=venue,
            entry_date=payload.entry_date,
            amount=payload.amount,
            note=payload.note,
            actor_user_id=request.state.user["id"],
        )
        return JSONResponse({
            "status": "ok",
            "message": "数据未变化" if saved["unchanged"] else "进场货款已保存",
            "entry": saved,
        })
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("保存进场货款失败: %s", exc)
        return JSONResponse({"error": "保存进场货款失败"}, status_code=500)


@app.get("/api/payment-accounting/month/{year}/{month}")
def payment_accounting_month(year: int, month: int, request: Request) -> JSONResponse:
    """严格读取指定月份最后一个自然日导入的累计基础货款。"""
    try:
        return JSONResponse(payment_accounting.get_monthly_base_payment(year, month, _venue_scope_for(request)))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.exception("读取月末基础货款失败: %s", exc)
        return JSONResponse({"error": "读取月末基础货款失败"}, status_code=500)


@app.get("/api/payment-accounting/summary")
def payment_accounting_summary(request: Request) -> JSONResponse:
    """汇总进场货款、严格月末基础货款和严格月末净积分。"""
    try:
        return JSONResponse(payment_accounting.get_lifetime_summary(venue_scope=_venue_scope_for(request)))
    except Exception as exc:
        logger.exception("读取累计货款汇总失败: %s", exc)
        return JSONResponse({"error": "读取累计货款汇总失败"}, status_code=500)


# ==================== API: 出货货款（独立报表） ====================

@app.get("/api/payment-accounting/outbound/months")
def outbound_months(source_store: str = "", request: Request = None):
    return outbound_payments.list_months(source_store, _venue_scope_for(request))


@app.get("/api/payment-accounting/outbound/overview")
def outbound_overview(month: str, source_store: str = "", request: Request = None):
    try:
        return outbound_payments.overview(month, source_store, _venue_scope_for(request))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.get("/api/payment-accounting/outbound/details")
def outbound_details(month: str, source_store: str = "", day: str = "", business_type: str = "设备出礼",
                     sku_id: str = "", equipment_no: str = "", attention: bool = False,
                     group: str = "records", page: int = 1, page_size: int = 50, request: Request = None):
    try:
        return outbound_payments.details(month, source_store, day, business_type, sku_id, equipment_no,
                                         attention, group, page, page_size, _venue_scope_for(request))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.get("/api/payment-accounting/outbound/status")
def outbound_status():
    return {**outbound_payments.status(), "credentials_configured": outbound_credentials_configured()}


@app.post("/api/payment-accounting/outbound/collect")
def outbound_collect(payload: dict, request: Request):
    if _venue_scope_for(request) is not None:
        raise HTTPException(status_code=403, detail="限定门店账号不能管理全公司采集批次")
    try:
        start = str(payload.get("start_month") or "")
        end = str(payload.get("end_month") or start)
        ids = outbound_manager.start(start, end, request.state.user["id"])
        return JSONResponse({"run_ids": ids, "message": "出货月度采集已启动"}, status_code=202)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/payment-accounting/outbound/runs/{run_id}/stop")
def outbound_stop(run_id: str, request: Request):
    if _venue_scope_for(request) is not None:
        raise HTTPException(status_code=403, detail="限定门店账号不能管理全公司采集批次")
    try:
        outbound_payments.cancel_batch(run_id)
        return {"message": "已停止该批次，旧完整版本保持不变"}
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/payment-accounting/outbound/runs/{run_id}/retry")
def outbound_retry(run_id: str, request: Request):
    if _venue_scope_for(request) is not None:
        raise HTTPException(status_code=403, detail="限定门店账号不能管理全公司采集批次")
    try:
        outbound_manager.retry(run_id)
        return JSONResponse({"message": "正在重试未完成日期"}, status_code=202)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


# ==================== API: 数据大屏 ====================

@app.get("/api/dashboard/status")
def dashboard_status(date: str = Query(...), request: Request = None) -> Dict:
    scope = _venue_scope_for(request)
    return get_dashboard_status(date) if scope is None else get_dashboard_status(date, scope)


@app.get("/api/collection/status")
def collection_status(date: str = Query(...)) -> Dict:
    """返回真实入库时间、数据源覆盖和自动采集排程，不暴露平台错误详情。"""
    try:
        return get_collection_freshness(date, scheduler=collection_auto_scheduler)
    except ValueError:
        return JSONResponse(
            {"error": "日期格式错误，请使用 YYYY-MM-DD"}, status_code=400
        )


@app.get("/api/dashboard/data")
def dashboard_data(date: str = Query(...), request: Request = None) -> Dict:
    scope = _venue_scope_for(request)
    return get_dashboard_data(date) if scope is None else get_dashboard_data(date, scope)


@app.get("/api/charts/platform-share")
def charts_platform_share(date: str = Query(...), request: Request = None) -> Dict:
    """本月各平台收入占比：按目标日月累计快照聚合（INCOME_COLUMNS 口径）。

    月累计快照本身即"月初至当日"口径，直接按平台求和即可得到本月各平台收入。
    """
    datetime.strptime(date, "%Y-%m-%d")
    scope = _venue_scope_for(request)
    income_columns = report_summary.INCOME_COLUMNS
    conn = get_connection()
    try:
        query = "SELECT platform, venue, metrics_json FROM daily_summary WHERE date=?"
        params: List[Any] = [date]
        if scope is not None:
            scope_set = {str(value).strip() for value in scope if str(value).strip()}
            if not scope_set:
                return {"date": date, "items": [], "total": 0}
            query += " AND venue IN (" + ",".join("?" for _ in scope_set) + ")"
            params.extend(sorted(scope_set))
        rows = conn.execute(query, tuple(params)).fetchall()
    finally:
        conn.close()
    totals: Dict[str, float] = {}
    for row in rows:
        try:
            metrics = json.loads(row["metrics_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(metrics, dict):
            continue
        platform_key = str(row["platform"] or "").strip() or "other"
        bucket = totals.setdefault(platform_key, 0.0)
        for field in income_columns:
            value = metrics.get(field)
            if isinstance(value, bool):
                continue
            try:
                bucket += float(value)
            except (TypeError, ValueError):
                continue
        totals[platform_key] = bucket
    configured = config_get("platforms", {}) or {}
    display_names = {
        str(pid): str((item or {}).get("name") or pid)
        for pid, item in configured.items()
        if isinstance(item, dict)
    } if isinstance(configured, dict) else {}
    items = [
        {"platform": pid, "name": display_names.get(pid, pid), "income": round(value, 2)}
        for pid, value in sorted(totals.items(), key=lambda kv: -kv[1])
        if value > 0
    ]
    return {
        "date": date,
        "items": items,
        "total": round(sum(item["income"] for item in items), 2),
    }


@app.get("/api/daily-operations")
def daily_operations(
    date: str = Query(...),
    venue: str = Query(""),
    request: Request = None,
) -> Dict:
    try:
        scope = _venue_scope_for(request)
        _ensure_venue_allowed(scope, venue)
        return get_daily_operations(date, venue, scope)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


# ==================== API: 会员资产监控 ====================

@app.get("/api/monitoring/overview")
def monitoring_overview(
    days: int = Query(7, ge=1, le=30),
    category: str = Query("all"),
    venue: str = Query(""),
    asset_group: str = Query("all"),
    asset_name: str = Query(""),
    change_type: str = Query(""),
    selected_date: str = Query("", alias="date"),
    request: Request = None,
) -> JSONResponse:
    """积分与会员储值统一看板；支持按单个业务日期核对完整流水。"""
    try:
        scope = _venue_scope_for(request)
        _ensure_venue_allowed(scope, venue)
        return JSONResponse(get_monitor_snapshot(
            days=days,
            category=category,
            venue=venue,
            target_date=selected_date or None,
            asset_group=asset_group,
            asset_name=asset_name,
            change_type=change_type,
            venue_scope=scope,
        ))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.get("/api/monitoring/sync/status")
def monitoring_sync_status() -> Dict[str, Any]:
    status = monitor_sync_manager.status()
    status["schedule"] = monitor_auto_scheduler.status()
    return status


@app.post("/api/monitoring/sync")
def monitoring_sync(
    start_date: str = Query(...),
    end_date: str = Query(...),
) -> JSONResponse:
    """后台同步多金宝会员储值变更，不在接口响应线程中执行长任务。"""
    try:
        started = monitor_sync_manager.start(start_date, end_date)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.exception("启动会员储值同步失败")
        return JSONResponse({"error": "启动同步失败：%s" % str(exc)}, status_code=500)
    if not started:
        return JSONResponse(
            {"error": "已有储值同步任务正在运行"}, status_code=409
        )
    return JSONResponse(monitor_sync_manager.status(), status_code=202)


# ==================== API: 数据质量中心 ====================

@app.get("/api/quality/{date}")
def data_quality(date: str, request: Request) -> JSONResponse:
    """指定日期的数据完整性、累计回退与收入异常检查。"""
    try:
        return JSONResponse(inspect_data_quality(date, _venue_scope_for(request)))
    except ValueError:
        return JSONResponse(
            {"error": "日期格式错误，请使用 YYYY-MM-DD"}, status_code=400
        )
    except Exception as e:
        logger.exception("数据质量检查失败")
        return JSONResponse({"error": f"检查失败：{e}"}, status_code=500)


@app.post("/api/quality/collect-missing")
def data_quality_collect_missing(payload: dict) -> JSONResponse:
    """补采指定日期缺失入库或最新任务失败的平台。"""
    date = str(payload.get("date") or "").strip()
    try:
        datetime.strptime(date, "%Y-%m-%d")
        report = inspect_data_quality(date)
    except ValueError:
        return JSONResponse(
            {"error": "日期格式错误，请使用 YYYY-MM-DD"}, status_code=400
        )
    except Exception as e:
        logger.exception("读取待补采平台失败")
        return JSONResponse({"error": f"检查失败：{e}"}, status_code=500)

    retry_platforms = set(
        report.get("retry_platforms", report.get("missing_platforms", []))
    )
    if not retry_platforms:
        return JSONResponse({
            "status": "ok",
            "message": "该日期所有配置平台均已成功入库，无需补采",
            "platforms": [],
        })
    adapters = [
        a for a in collection_settings.filter_enabled_adapters(build_adapters(), PLATFORM_LIST)
        if a.platform_name in retry_platforms
    ]
    if not adapters:
        return JSONResponse({"error": "未找到可补采的平台适配器"}, status_code=400)

    scheduler = Scheduler(
        task_mgr=TaskManager(),
        max_workers=min(SCHEDULER_WORKERS, len(adapters)),
        single_task_timeout=SCHEDULER_TIMEOUT,
        process_isolation=SCHEDULER_PROCESS_ISOLATION,
    )
    month_start = datetime.strptime(date, "%Y-%m-%d").date().replace(day=1).isoformat()
    if not scheduler.launch_background(month_start, date, adapters):
        return JSONResponse(
            {"status": "busy", "message": "上一批采集仍在进行中，请稍后再试"},
            status_code=409,
        )
    logger.info(
        "质量中心缺失/失败平台补采启动: %s / %s",
        date,
        ",".join(sorted(retry_platforms)),
    )
    return JSONResponse({
        "status": "started",
        "date": date,
        "platforms": sorted(retry_platforms),
        "start_date": month_start,
        "message": f"已启动 {len(adapters)} 个缺失/失败平台的月累计补采",
    })


# ==================== API: 日志 ====================

@app.get("/api/logs/{date}")
def get_logs(date: str) -> List[Dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT platform, status, error_msg, created_at, date "
            "FROM tasks WHERE date=? ORDER BY created_at DESC",
            (date,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ==================== API: 凭证管理 ====================

@app.get("/api/credentials")
def get_credentials() -> Dict[str, Dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT platform, status, updated_at FROM credentials ORDER BY platform"
        ).fetchall()
        return {
            r["platform"]: {"status": r["status"], "updated": r["updated_at"]}
            for r in rows
        }
    finally:
        conn.close()


@app.post("/api/credentials/{platform}")
def save_credential(platform: str, data: dict) -> Dict:
    allowed = {name for name, _label in PLATFORM_LIST}
    if platform not in allowed:
        return JSONResponse({"error": "unknown platform"}, status_code=404)
    if not isinstance(data, dict):
        return JSONResponse({"error": "凭证数据格式错误"}, status_code=400)
    # 前端不会回显密文；空字段表示保留原值，避免编辑一个字段时清空其他密钥。
    existing = cred_mgr.load(platform) or {}
    merged = dict(existing)
    for key, value in data.items():
        if value is None:
            continue
        text = str(value)
        if text:
            merged[str(key)] = text
    if not merged:
        return JSONResponse({"error": "至少填写一个凭证字段"}, status_code=400)
    cred_mgr.save(platform, merged)
    return {"status": "saved"}


@app.get("/api/credentials/{platform}")
def get_credential(platform: str) -> Dict[str, Any]:
    """读取凭证元数据；绝不向浏览器返回解密后的密钥。"""
    allowed = {name for name, _label in PLATFORM_LIST}
    if platform not in allowed:
        return JSONResponse({"error": "unknown platform"}, status_code=404)
    cred = cred_mgr.load(platform) or {}
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT status, updated_at FROM credentials WHERE platform=?", (platform,)
        ).fetchone()
    finally:
        conn.close()
    return {
        "platform": platform,
        "configured": bool(cred),
        "configured_fields": [key for key, value in cred.items() if value],
        "status": row["status"] if row else "unknown",
        "updated": row["updated_at"] if row else None,
    }


# ==================== API: AI 分析师 ====================

@app.post("/api/analyst/ask")
def analyst_ask(payload: dict) -> JSONResponse:
    """AI 分析师问答：同步执行工具调用循环后返回最终回答"""
    question = str(payload.get("question") or "").strip()
    session_id = str(payload.get("session_id") or "")
    if not question:
        return JSONResponse({"error": "问题不能为空"}, status_code=400)
    try:
        result = run_question(question, session_id=session_id)
        return result
    except Exception as e:
        logger.error("AI 分析师调用失败: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/analyst/ask/stream")
def analyst_ask_stream(payload: dict) -> StreamingResponse:
    """AI 分析师流式问答：SSE 事件流（meta / tool / delta / done / error）"""
    question = str(payload.get("question") or "").strip()
    session_id = str(payload.get("session_id") or "")

    def event_source():
        try:
            for evt_type, evt_payload in run_question_stream(question, session_id):
                yield "data: " + json.dumps(
                    {"type": evt_type, **evt_payload},
                    ensure_ascii=False,
                ) + "\n\n"
        except Exception as e:
            logger.error("AI 分析师流式调用失败: %s", e)
            yield "data: " + json.dumps(
                {"type": "error", "error": str(e)},
                ensure_ascii=False,
            ) + "\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/analyst/config")
def analyst_config() -> Dict:
    """返回当前模型配置（供前端展示，不含密钥）"""
    return {
        "provider": analyst_llm.get_provider(),
        "model": analyst_llm.get_model(),
        "base_url": analyst_llm.get_base_url(),
        "configured": analyst_llm.is_configured(),
    }


@app.get("/api/analyst/sessions")
def analyst_sessions() -> Dict:
    """历史会话列表"""
    return {"sessions": list_sessions()}


@app.get("/api/analyst/session/{session_id}")
def analyst_session_messages(session_id: str) -> Dict:
    """读取单个会话的消息记录"""
    return {"session_id": session_id, "messages": get_session_messages(session_id)}


@app.get("/api/analyst/forecast")
def analyst_forecast(date: str = Query(...), horizon: int = Query(3), request: Request = None) -> Dict:
    """预测引擎：本月完成率推演 + 月度趋势预测"""
    try:
        return combined_forecast(date, horizon=horizon, venue_scope=_venue_scope_for(request))
    except Exception as e:
        logger.error("预测接口失败: %s", e)
        return {"error": str(e)}


@app.get("/api/analyst/forecast/anomaly")
def analyst_forecast_anomaly(
    date: str = Query(...),
    threshold: float = Query(2.0),
    request: Request = None,
) -> Dict:
    """单日收入异常检测"""
    try:
        return detect_anomaly(date, threshold=threshold, venue_scope=_venue_scope_for(request))
    except Exception as e:
        logger.error("异常检测接口失败: %s", e)
        return {"error": str(e)}


@app.get("/api/analyst/targets")
def analyst_targets_get(month: str = Query(...)) -> Dict:
    """查看某月已导入的每月目标（老板报表「每月目标」子表数据源）"""
    if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
        return JSONResponse({"error": "月份格式错误，请使用 YYYY-MM"}, status_code=400)
    targets = load_store_targets(month)
    source_name = target_source_name(month)
    system_names = system_venue_names()
    unmatched = sorted(venue for venue in targets if venue not in system_names)
    return {
        "month": month,
        "file": source_name,
        "count": len(targets),
        "total": round(sum(targets.values()), 2),
        "targets": [
            {"venue": venue, "target": value}
            for venue, value in sorted(targets.items())
        ],
        "unmatched": unmatched,
        "note": (
            "未匹配门店：目标表中的名称与系统门店名不一致，"
            "老板报表将按名称匹配不到目标（完成率显示 0），"
            "请核对目标表门店名或配置 aliases"
            if unmatched else ""
        ),
    }


@app.post("/api/analyst/targets/import")
def analyst_targets_import(
    file: UploadFile = File(...),
    month: str = Form(""),
    request: Request = None,
) -> JSONResponse:
    """导入每月目标表（.xlsx）：校验后保存为该月目标来源，供预测与老板报表使用"""
    if _venue_scope_for(request) is not None:
        return JSONResponse({"error": "限定门店账号不能导入每月目标"}, status_code=403)

    filename = file.filename or ""
    if not filename.lower().endswith(".xlsx"):
        return JSONResponse({"error": "请上传 .xlsx 格式的目标表"}, status_code=400)

    month = (month or "").strip() or infer_month_from_filename(filename)
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        return JSONResponse(
            {"error": "无法确定目标月份：请在表单中选择月份，或让文件名包含年月（如 2026-10）"},
            status_code=400,
        )

    try:
        content = file.file.read(MAX_UPLOAD_BYTES + 1)
    finally:
        file.file.close()
    if len(content) > MAX_UPLOAD_BYTES:
        return JSONResponse({"error": "目标表文件不能超过 10MB"}, status_code=400)

    try:
        targets = parse_target_workbook(content)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"目标表解析失败：{e}"}, status_code=400)

    try:
        save_target_file(month, content)
    except Exception as e:
        logger.error("每月目标保存失败: %s", e)
        return JSONResponse({"error": f"保存目标表失败：{e}"}, status_code=500)

    system_names = system_venue_names()
    unmatched = sorted(venue for venue in targets if venue not in system_names)
    return {
        "status": "ok",
        "month": month,
        "file": f"{month}_区域门店目标汇总表.xlsx",
        "count": len(targets),
        "total": round(sum(targets.values()), 2),
        "unmatched": unmatched,
        "message": (
            f"已导入 {month} 每月目标：{len(targets)} 家门店，"
            f"合计 ¥{round(sum(targets.values()), 2):,.2f}"
            + (f"；注意 {len(unmatched)} 家门店名称未匹配到系统门店" if unmatched else "")
        ),
    }


# ==================== API: 双入口命名空间 ====================

def _mount_api_catalog(
    prefix: str,
    catalog: Tuple[Tuple[str, str], ...],
    tag: str,
) -> None:
    """为现有兼容路由增加分区明确的新路径，不复制业务实现。"""
    endpoints = {
        (method, route.path): route.endpoint
        for route in app.routes
        for method in (getattr(route, "methods", None) or set())
        if hasattr(route, "endpoint")
    }
    for method, legacy_path in catalog:
        endpoint = endpoints.get((method, legacy_path))
        if endpoint is None:
            raise RuntimeError(f"API 边界清单引用了不存在的路由: {method} {legacy_path}")
        relative_path = legacy_path[len("/api"):]
        app.add_api_route(
            prefix + relative_path,
            endpoint,
            methods=[method],
            name=f"{tag}_{endpoint.__name__}",
            tags=[tag],
        )


_mount_api_catalog(PORTAL_API_PREFIX, PORTAL_API_ROUTES + PORTAL_ACTION_ROUTES, "portal")
_mount_api_catalog(ADMIN_API_PREFIX, ADMIN_API_ROUTES, "admin")


# ==================== 启动 ====================

if __name__ == "__main__":
    import uvicorn
    # 管理界面默认仅供本机访问，避免凭证接口暴露到局域网。
    uvicorn.run(app, host="127.0.0.1", port=8010, access_log=False, log_config=None)
