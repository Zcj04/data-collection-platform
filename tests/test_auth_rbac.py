# -*- coding: utf-8 -*-
"""身份认证与 RBAC 契约测试；可直接用 Python 运行，不依赖 pytest。"""

import asyncio
import hashlib
import importlib
import inspect
import os
import logging
import sys
import tempfile
import threading
import unittest
import warnings
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PYLIB = ROOT / "_pylib"
BASE_ORIGIN = "https://testserver"

warnings.filterwarnings("ignore", category=DeprecationWarning)
logging.getLogger("httpx2").setLevel(logging.WARNING)

if sys.version_info < (3, 10):
    raise SystemExit("认证测试需要项目自带 Python 3.13 运行时（参见 docs/操作手册.md）")

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 项目运行时提供 httpx2，但 Starlette TestClient 仍按 httpx 名称导入。
try:
    import httpx  # noqa: F401
except ImportError:
    if PYLIB.is_dir() and str(PYLIB) not in sys.path:
        sys.path.insert(0, str(PYLIB))
    import httpx2

    httpx2.alias_httpx()

from fastapi.testclient import TestClient


ADMIN_USERNAME = "owner.admin"
ADMIN_PASSWORD = "Owner-password-2026"
VIEWER_PASSWORD = "Viewer-password-2026"
RESET_PASSWORD = "Viewer-reset-2026"


class AuthRbacContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temp_dir = tempfile.TemporaryDirectory(prefix="workbuddy-auth-test-")
        cls._temp_root = Path(cls._temp_dir.name)

        import core.db as db
        import core.backup as backup
        import core.credential_manager as credential_manager

        cls.db = db
        cls._original_db_path = db.DB_PATH
        cls._original_key_file = credential_manager.KEY_FILE
        cls._original_backup_root = backup._configured_root
        cls._original_log_file = os.environ.get("WORKBUDDY_LOG_FILE")
        db.DB_PATH = str(cls._temp_root / "app.db")
        credential_manager.KEY_FILE = str(cls._temp_root / "credentials" / ".fernet_key")
        backup._configured_root = lambda: cls._temp_root / "backups" / "automatic"
        os.environ["WORKBUDDY_LOG_FILE"] = str(cls._temp_root / "logs" / "app.log")

        # app_fastapi 导入时会初始化数据库与凭证管理器；以上路径必须先替换。
        sys.modules.pop("app_fastapi", None)
        cls.app_module = importlib.import_module("app_fastapi")
        cls.auth = cls.app_module.auth_service
        cls.app = cls.app_module.app

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("app_fastapi", None)
        from core.process_lock import release_single_process_lock

        release_single_process_lock(cls.db.DB_PATH)
        cls.db.DB_PATH = cls._original_db_path
        import core.credential_manager as credential_manager
        import core.backup as backup
        from core.logging import close_log_file

        credential_manager.KEY_FILE = cls._original_key_file
        backup._configured_root = cls._original_backup_root
        close_log_file(str(cls._temp_root / "logs" / "app.log"))
        if cls._original_log_file is None:
            os.environ.pop("WORKBUDDY_LOG_FILE", None)
        else:
            os.environ["WORKBUDDY_LOG_FILE"] = cls._original_log_file
        cls._temp_dir.cleanup()

    def setUp(self):
        conn = self.db.get_connection()
        try:
            conn.execute("DELETE FROM auth_sessions")
            conn.execute("DELETE FROM auth_login_attempts")
            conn.execute("DELETE FROM users")
            conn.execute("DELETE FROM venue_lifecycle")
            conn.commit()
        finally:
            conn.close()
        self.client = self._new_client()

    def _new_client(self, *, client_address=("testclient", 50000)):
        client = TestClient(
            self.app,
            base_url=BASE_ORIGIN,
            follow_redirects=False,
            client=client_address,
        )
        self.addCleanup(client.close)
        return client

    @staticmethod
    def _headers(csrf_token=None, origin=BASE_ORIGIN):
        headers = {}
        if csrf_token is not None:
            headers["X-CSRF-Token"] = csrf_token
        if origin is not None:
            headers["Origin"] = origin
        return headers

    def _setup_admin(self, client=None):
        target = client or self.client
        response = target.post(
            "/api/auth/setup",
            json={
                "username": ADMIN_USERNAME,
                "display_name": "平台管理员",
                "password": ADMIN_PASSWORD,
            },
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _login(self, username, password, client=None):
        target = client or self.client
        response = target.post(
            "/api/auth/login",
            json={"username": username, "password": password},
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _create_user(
        self,
        csrf_token,
        *,
        username="data.viewer",
        display_name="数据查看者",
        password=VIEWER_PASSWORD,
        permissions=None,
        position="",
        scope_type="all",
        venues=None,
        role_key="custom",
    ):
        response = self.client.post(
            "/api/admin/users",
            json={
                "username": username,
                "display_name": display_name,
                "password": password,
                "permissions": permissions or ["portal.view"],
                "position": position,
                "scope_type": scope_type,
                "venues": venues,
                "role_key": role_key,
            },
            headers=self._headers(csrf_token),
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["user"]

    def _scalar(self, query, params=()):
        conn = self.db.get_connection()
        try:
            row = conn.execute(query, params).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def test_setup_rejects_forwarded_requests_even_from_loopback_proxy(self):
        client = self._new_client(client_address=("127.0.0.1", 50100))
        for header in ("X-Forwarded-For", "Forwarded", "X-Real-IP", "X-Forwarded-Proto"):
            with self.subTest(header=header):
                response = client.post("/api/auth/setup", headers={header: "192.0.2.10"},
                                       json={"username": ADMIN_USERNAME, "display_name": "Admin", "password": ADMIN_PASSWORD})
                self.assertEqual(response.status_code, 403)
                self.assertEqual(self.auth.user_count(), 0)

    def test_setup_is_local_single_use_and_stores_no_plaintext_password(self):
        remote = self._new_client(client_address=("192.0.2.10", 50100))
        payload = {
            "username": ADMIN_USERNAME,
            "display_name": "平台管理员",
            "password": ADMIN_PASSWORD,
        }

        live = self.client.get("/health/live")
        ready = self.client.get("/health/ready")
        self.assertEqual(live.status_code, 200, live.text)
        self.assertEqual(live.json(), {"status": "alive"})
        self.assertEqual(ready.status_code, 200, ready.text)
        self.assertEqual(ready.json()["status"], "ready")
        self.assertEqual(live.headers.get("cache-control"), "no-store")

        remote_response = remote.post(
            "/api/auth/setup", json=payload, headers=self._headers()
        )
        self.assertEqual(remote_response.status_code, 403)

        cross_origin = self.client.post(
            "/api/auth/setup",
            json=payload,
            headers=self._headers(origin="https://evil.example"),
        )
        self.assertEqual(cross_origin.status_code, 403)

        response = self.client.post(
            "/api/auth/setup", json=payload, headers=self._headers()
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(set(body["user"]["permissions"]), {
            "portal.view",
            "portal.download",
            "boss_report.download",
            "monitoring.view",
            "payment.view",
            "payment.manage",
            "admin.access",
            "users.manage",
        })

        set_cookie = response.headers.get("set-cookie", "")
        self.assertIn("workbuddy_session=", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("Secure", set_cookie)
        self.assertIn("SameSite=strict", set_cookie)
        self.assertIn("Path=/", set_cookie)
        self.assertNotIn("Domain=", set_cookie)

        password_hash = self._scalar(
            "SELECT password_hash FROM users WHERE username=?", (ADMIN_USERNAME,)
        )
        self.assertTrue(password_hash.startswith("pbkdf2_sha256$600000$"))
        self.assertNotEqual(password_hash, ADMIN_PASSWORD)
        self.assertNotIn(ADMIN_PASSWORD, password_hash)

        cookie_token = self.client.cookies.get(self.auth.SESSION_COOKIE_NAME)
        stored_token_hash = self._scalar("SELECT token_hash FROM auth_sessions")
        self.assertTrue(cookie_token)
        self.assertEqual(stored_token_hash, hashlib.sha256(cookie_token.encode()).hexdigest())
        self.assertNotEqual(stored_token_hash, cookie_token)
        self.assertNotIn(cookie_token, response.text)

        repeated = self.client.post(
            "/api/auth/setup", json=payload, headers=self._headers()
        )
        self.assertEqual(repeated.status_code, 409)

    def test_login_rotates_session_and_uses_generic_failure(self):
        self._setup_admin()
        login_client = self._new_client()

        wrong_user = login_client.post(
            "/api/auth/login",
            json={"username": "missing.user", "password": "wrong-password"},
            headers=self._headers(),
        )
        wrong_password = login_client.post(
            "/api/auth/login",
            json={"username": ADMIN_USERNAME, "password": "wrong-password"},
            headers=self._headers(),
        )
        self.assertEqual(wrong_user.status_code, 401)
        self.assertEqual(wrong_password.status_code, 401)
        self.assertEqual(wrong_user.json()["detail"], wrong_password.json()["detail"])

        login_response = login_client.post(
            "/api/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
            headers={
                **self._headers(),
                "Cookie": "%s=fixed-by-attacker" % self.auth.SESSION_COOKIE_NAME,
            },
        )
        self.assertEqual(login_response.status_code, 200, login_response.text)
        body = login_response.json()
        issued_token = login_client.cookies.get(self.auth.SESSION_COOKIE_NAME)
        self.assertNotEqual(issued_token, "fixed-by-attacker")
        self.assertIn("csrf_token", body)
        self.assertNotIn(issued_token, str(body))

        me = login_client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(me.json()["user"]["username"], ADMIN_USERNAME)
        self.assertEqual(me.json()["csrf_token"], body["csrf_token"])

    def test_login_throttle_persists_and_returns_retry_after(self):
        self._setup_admin()
        client = self._new_client(client_address=("192.0.2.44", 50123))
        for attempt in range(5):
            response = client.post(
                "/api/auth/login",
                json={"username": ADMIN_USERNAME, "password": "wrong-password"},
                headers=self._headers(),
            )
            self.assertEqual(response.status_code, 429 if attempt == 4 else 401)
        self.assertGreaterEqual(int(response.headers["retry-after"]), 1)

        blocked = client.post(
            "/api/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
            headers=self._headers(),
        )
        self.assertEqual(blocked.status_code, 429)

        other_source = self._new_client(client_address=("192.0.2.45", 50124))
        allowed = other_source.post(
            "/api/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
            headers=self._headers(),
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)

    def test_security_headers_cover_public_auth_health_and_errors(self):
        for response in (
            self.client.get("/login"),
            self.client.get("/health/live"),
            self.client.get("/api/auth/me"),
        ):
            self.assertEqual(response.headers.get("x-content-type-options"), "nosniff")
            self.assertEqual(response.headers.get("x-frame-options"), "DENY")
            self.assertEqual(response.headers.get("referrer-policy"), "no-referrer")
            self.assertIn("frame-ancestors 'none'", response.headers.get("content-security-policy", ""))
        self.assertIn("strict-transport-security", self.client.get("/login").headers)

    def test_expired_session_is_rejected_and_removed_server_side(self):
        self._setup_admin()
        session_token = self.client.cookies.get(self.auth.SESSION_COOKIE_NAME)
        token_hash = hashlib.sha256(session_token.encode()).hexdigest()
        conn = self.db.get_connection()
        try:
            conn.execute(
                "UPDATE auth_sessions SET expires_at=? WHERE token_hash=?",
                ("2000-01-01T00:00:00+00:00", token_hash),
            )
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)
        self.assertEqual(
            self._scalar(
                "SELECT COUNT(*) FROM auth_sessions WHERE token_hash=?",
                (token_hash,),
            ),
            0,
        )

    def test_password_hash_uses_unique_salts_and_fails_closed(self):
        first = self.auth.hash_password(ADMIN_PASSWORD)
        second = self.auth.hash_password(ADMIN_PASSWORD)
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("pbkdf2_sha256$600000$"))
        self.assertTrue(self.auth.verify_password(ADMIN_PASSWORD, first))
        self.assertFalse(self.auth.verify_password("incorrect-password", first))
        self.assertFalse(self.auth.verify_password(ADMIN_PASSWORD, "broken-hash"))

    def test_pages_and_api_apply_anonymous_viewer_admin_matrix(self):
        admin_session = self._setup_admin()
        viewer = self._create_user(admin_session["csrf_token"])
        admin_user = self._create_user(
            admin_session["csrf_token"],
            username="ops.admin",
            display_name="后台管理员",
            permissions=["portal.view", "admin.access"],
        )

        anonymous = self._new_client()
        for path in ("/", "/portal", "/store", "/admin"):
            with self.subTest(actor="anonymous", path=path):
                response = anonymous.get(path)
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers.get("location"), "/login")
        for path in (
            "/api/platform-links",
            "/api/portal/platform-links",
            "/api/summary/2099-01-01",
            "/api/portal/summary/2099-01-01",
            "/api/collection/status?date=2099-01-01",
            "/api/portal/collection/status?date=2099-01-01",
            "/api/tasks/2099-01-01",
            "/api/admin/tasks/2099-01-01",
            "/api/admin/venues",
        ):
            with self.subTest(actor="anonymous", path=path):
                self.assertEqual(anonymous.get(path).status_code, 401)

        viewer_client = self._new_client()
        self._login(viewer["username"], VIEWER_PASSWORD, viewer_client)
        self.assertEqual(viewer_client.get("/").status_code, 200)
        self.assertEqual(viewer_client.get("/portal").status_code, 200)
        self.assertEqual(viewer_client.get("/admin").status_code, 403)

        admin_client = self._new_client()
        self._login(admin_user["username"], VIEWER_PASSWORD, admin_client)
        self.assertEqual(admin_client.get("/").status_code, 200)
        self.assertEqual(admin_client.get("/portal").status_code, 200)
        self.assertEqual(admin_client.get("/admin").status_code, 200)
        self.assertEqual(admin_client.get("/api/admin/users").status_code, 403)
        self.assertEqual(admin_client.get("/api/admin/venues").status_code, 200)

        self.assertEqual(self.client.get("/admin").status_code, 200)
        self.assertEqual(self.client.get("/api/admin/users").status_code, 200)
        venue_response = self.client.get("/api/admin/venues")
        self.assertEqual(venue_response.status_code, 200)
        self.assertIsInstance(venue_response.json()["venues"], list)
        self.assertIsInstance(venue_response.json()["items"], list)

    def test_scoped_payment_manager_cannot_change_global_batches(self):
        self._setup_admin()
        self.auth.create_user("limited.pay", "Limited", VIEWER_PASSWORD,
                              ["payment.manage"], scope_type="venues", venues=["A"])
        session = self._login("limited.pay", VIEWER_PASSWORD)
        for prefix in ("/api", "/api/admin"):
            for suffix in ("collect", "runs/example/stop", "runs/example/retry"):
                response = self.client.post(prefix + "/payment-accounting/outbound/" + suffix,
                                            json={}, headers=self._headers(session["csrf_token"]))
                self.assertEqual(response.status_code, 403)

    def test_boss_reports_use_independent_files_and_cleanup(self):
        from concurrent.futures import ThreadPoolExecutor
        import io
        import openpyxl
        from types import SimpleNamespace
        session = self._setup_admin()
        barrier = threading.Barrier(2)
        paths = []
        def generate(date, output_path=None, venue_scope=None):
            self.assertIsNotNone(output_path)
            paths.append(output_path)
            barrier.wait(timeout=10)
            workbook = openpyxl.Workbook()
            workbook.active["A1"] = date
            workbook.save(output_path)
            return output_path
        request = SimpleNamespace(state=SimpleNamespace(user=session["user"]))
        async def body(response):
            return b"".join([part async for part in response.body_iterator])
        with mock.patch.object(self.app_module, "generate_boss_report", side_effect=generate):
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(lambda _: self.app_module.boss_report("2026-09-07", request), range(2)))
        self.assertEqual(len(set(paths)), 2)
        self.assertTrue(all(not os.path.exists(path) for path in paths))
        for response in responses:
            workbook = openpyxl.load_workbook(io.BytesIO(asyncio.run(body(response))))
            self.assertEqual(workbook.active["A1"].value, "2026-09-07")
            workbook.close()

    def test_legacy_and_namespaced_api_have_the_same_permission_boundary(self):
        admin_session = self._setup_admin()
        viewer = self._create_user(admin_session["csrf_token"])
        viewer_client = self._new_client()
        viewer_session = self._login(
            viewer["username"], VIEWER_PASSWORD, viewer_client
        )

        for path in (
            "/api/platform-links",
            "/api/portal/platform-links",
            "/api/summary/2099-01-01",
            "/api/portal/summary/2099-01-01",
            "/api/collection/status?date=2099-01-01",
            "/api/portal/collection/status?date=2099-01-01",
        ):
            with self.subTest(kind="portal", path=path):
                self.assertEqual(viewer_client.get(path).status_code, 200)

        platform_payload = viewer_client.get("/api/portal/platform-links").json()
        self.assertEqual(len(platform_payload.get("platforms", [])), 13)
        serialized = repr(platform_payload).lower()
        for forbidden in ("password", "cookie", "token", "file_path", "mtgsig"):
            self.assertNotIn(forbidden, serialized)

        for path in (
            "/api/download/2099-01-01",
            "/api/portal/download/2099-01-01",
            "/api/tasks/2099-01-01",
            "/api/admin/tasks/2099-01-01",
        ):
            with self.subTest(kind="forbidden", path=path):
                self.assertEqual(viewer_client.get(path).status_code, 403)

        for path in (
            "/api/collect?start_date=2099-01-01&end_date=2099-01-01",
            "/api/admin/collect?start_date=2099-01-01&end_date=2099-01-01",
        ):
            with self.subTest(kind="write-bypass", path=path):
                response = viewer_client.post(
                    path, headers=self._headers(viewer_session["csrf_token"])
                )
                self.assertEqual(response.status_code, 403)

        for path in (
            "/api/tasks/2099-01-01",
            "/api/admin/tasks/2099-01-01",
        ):
            with self.subTest(kind="admin", path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

        self.assertEqual(viewer_client.get("/api/not-catalogued").status_code, 403)
        self.assertEqual(self.client.get("/api/not-catalogued").status_code, 404)

    def test_specialized_read_permissions_do_not_open_management_operations(self):
        admin_session = self._setup_admin()
        boss = self._create_user(
            admin_session["csrf_token"],
            username="finance.viewer",
            display_name="财务查看者",
            permissions=[
                "portal.view", "portal.download", "boss_report.download",
                "monitoring.view", "payment.view",
            ],
        )
        payment = self._create_user(
            admin_session["csrf_token"],
            username="payment.operator",
            display_name="货款管理员",
            permissions=["payment.manage"],
        )

        boss_client = self._new_client()
        self._login(boss["username"], VIEWER_PASSWORD, boss_client)
        for path in (
            "/api/boss-report/check/2099-01-01",
            "/api/portal/boss-report/check/2099-01-01",
            "/api/admin/boss-report/check/2099-01-01",
            "/api/monitoring/overview",
            "/api/portal/monitoring/overview",
            "/api/admin/monitoring/overview",
            "/api/monitoring/sync/status",
            "/api/quality/2099-01-01",
            "/api/payment-data/years",
            "/api/portal/payment-data/years",
            "/api/admin/payment-data/years",
            "/api/payment-accounting/summary",
            "/api/portal/payment-accounting/summary",
            "/api/admin/payment-accounting/summary",
        ):
            with self.subTest(actor="boss", path=path):
                self.assertNotEqual(boss_client.get(path).status_code, 403)
        for method, path in (
            ("POST", "/api/collect"),
            ("POST", "/api/monitoring/sync"),
            ("POST", "/api/quality/collect-missing"),
            ("POST", "/api/payment-data/upload"),
            ("DELETE", "/api/payment-data/date/2099-01-01"),
            ("GET", "/api/credentials"),
            ("GET", "/api/tasks/2099-01-01"),
        ):
            with self.subTest(actor="boss", method=method, path=path):
                response = boss_client.request(method, path, headers=self._headers(
                    boss_client.cookies.get(self.auth.SESSION_COOKIE_NAME)
                ))
                self.assertEqual(response.status_code, 403)

        payment_client = self._new_client()
        payment_session = self._login(payment["username"], VIEWER_PASSWORD, payment_client)
        self.assertEqual(payment_session["redirect"], "/admin#paymentdata")
        self.assertEqual(payment_client.get("/admin").status_code, 200)
        self.assertEqual(payment_client.get("/api/payment-data/years").status_code, 200)
        self.assertEqual(payment_client.get("/api/admin/payment-data/years").status_code, 200)
        for path in (
            "/api/portal/payment-data/years",
            "/api/portal/payment-data/date/2099-01-01",
            "/api/portal/payment-accounting/summary",
        ):
            with self.subTest(actor="payment", path=path):
                self.assertNotEqual(payment_client.get(path).status_code, 403)
        self.assertEqual(payment_client.get("/api/portal/monitoring/overview").status_code, 403)
        self.assertEqual(payment_client.get("/api/portal/boss-report/check/2099-01-01").status_code, 403)
        self.assertEqual(payment_client.get("/api/monitoring/overview").status_code, 403)
        self.assertEqual(payment_client.get("/api/tasks/2099-01-01").status_code, 403)
        self.assertEqual(payment_client.get("/api/admin/venues").status_code, 403)
        self.assertEqual(payment_client.get("/api/credentials").status_code, 403)

    def test_csrf_blocks_writes_before_state_change_and_logout_revokes_session(self):
        admin_session = self._setup_admin()
        create_payload = {
            "username": "csrf.viewer",
            "display_name": "CSRF 查看者",
            "password": VIEWER_PASSWORD,
            "permissions": ["portal.view"],
        }

        attempts = (
            {},
            self._headers("wrong-token"),
            self._headers(
                admin_session["csrf_token"], origin="https://evil.example"
            ),
        )
        for headers in attempts:
            with self.subTest(headers=headers):
                response = self.client.post(
                    "/api/admin/users", json=create_payload, headers=headers
                )
                self.assertEqual(response.status_code, 403)
                self.assertEqual(self._scalar("SELECT COUNT(*) FROM users"), 1)

        created = self.client.post(
            "/api/admin/users",
            json=create_payload,
            headers=self._headers(admin_session["csrf_token"]),
        )
        self.assertEqual(created.status_code, 201, created.text)

        for path in (
            "/api/payment-data/date/2099-01-01",
            "/api/admin/payment-data/date/2099-01-01",
        ):
            with self.subTest(method="DELETE", path=path):
                self.assertEqual(self.client.delete(path).status_code, 403)

        self.assertEqual(self.client.post("/api/auth/logout").status_code, 403)
        session_token = self.client.cookies.get(self.auth.SESSION_COOKIE_NAME)
        logout = self.client.post(
            "/api/auth/logout",
            headers=self._headers(admin_session["csrf_token"]),
        )
        self.assertEqual(logout.status_code, 200, logout.text)
        self.assertNotIn(self.auth.SESSION_COOKIE_NAME, self.client.cookies)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

        replay = self._new_client()
        replay_response = replay.get(
            "/api/auth/me",
            headers={"Cookie": "%s=%s" % (self.auth.SESSION_COOKIE_NAME, session_token)},
        )
        self.assertEqual(replay_response.status_code, 401)

    def test_payment_accounting_routes_keep_admin_and_csrf_boundary(self):
        admin_session = self._setup_admin()
        viewer = self._create_user(admin_session["csrf_token"])
        viewer_client = self._new_client()
        self._login(viewer["username"], VIEWER_PASSWORD, viewer_client)

        entry_payload = {
            "summary": {"venue_count": 1, "filled_count": 0, "total": 0},
            "rows": [],
        }
        month_payload = {
            "status": "missing",
            "month_end_date": "2026-08-31",
            "rows": [],
            "unmatched": [],
        }
        summary_payload = {
            "summary": {"venue_count": 1, "incomplete_count": 1, "known_total": 0},
            "months": [],
            "rows": [],
        }
        with mock.patch.object(
            self.app_module.payment_accounting,
            "list_entry_payments",
            return_value=entry_payload,
        ), mock.patch.object(
            self.app_module.payment_accounting,
            "get_monthly_base_payment",
            return_value=month_payload,
        ), mock.patch.object(
            self.app_module.payment_accounting,
            "get_lifetime_summary",
            return_value=summary_payload,
        ):
            for path in (
                "/api/payment-accounting/entry",
                "/api/admin/payment-accounting/entry",
            ):
                self.assertEqual(self.client.get(path).status_code, 200)
                self.assertEqual(viewer_client.get(path).status_code, 403)
            for path in (
                "/api/payment-accounting/month/2026/8",
                "/api/admin/payment-accounting/month/2026/8",
            ):
                self.assertEqual(self.client.get(path).status_code, 200)
                self.assertEqual(viewer_client.get(path).status_code, 403)
            for path in (
                "/api/payment-accounting/summary",
                "/api/admin/payment-accounting/summary",
            ):
                self.assertEqual(self.client.get(path).status_code, 200)
                self.assertEqual(viewer_client.get(path).status_code, 403)

        save_path = "/api/admin/payment-accounting/entry/%E6%B7%B1%E5%9C%B3A%E5%BA%97"
        body = {"entry_date": "2026-01-01", "amount": 1000, "note": "首批"}
        with mock.patch.object(
            self.app_module.payment_accounting,
            "save_entry_payment",
            return_value={
                "venue": "深圳A店", "revision": 1, "entry_date": "2026-01-01",
                "amount": 1000.0, "note": "首批", "actor_user_id": "admin",
                "updated_at": "2026-08-27 10:00:00", "unchanged": False,
            },
        ) as save:
            self.assertEqual(self.client.post(save_path, json=body).status_code, 403)
            response = self.client.post(
                save_path,
                json=body,
                headers=self._headers(admin_session["csrf_token"]),
            )
            self.assertEqual(response.status_code, 200, response.text)
            save.assert_called_once()

    def test_outbound_endpoints_require_admin_and_csrf(self):
        anonymous = self._new_client()
        admin_session = self._setup_admin()
        viewer = self._create_user(admin_session["csrf_token"])
        viewer_client = self._new_client()
        viewer_session = self._login(viewer["username"], VIEWER_PASSWORD, viewer_client)
        for prefix in ("/api", "/api/admin"):
            base = prefix + "/payment-accounting/outbound"
            for tail in ("/months", "/overview?month=2026-07", "/details?month=2026-07", "/status"):
                self.assertEqual(anonymous.get(base + tail).status_code, 401)
                self.assertEqual(viewer_client.get(base + tail).status_code, 403)
                self.assertEqual(self.client.get(base + tail).status_code, 200)
            for tail in ("/collect", "/runs/example/stop", "/runs/example/retry"):
                self.assertEqual(self.client.post(base + tail, json={}).status_code, 403)
                self.assertEqual(viewer_client.post(base + tail, json={}, headers=self._headers(viewer_session["csrf_token"])).status_code, 403)
            with mock.patch.object(self.app_module.outbound_manager, "start", return_value=["test-run"]) as start:
                response = self.client.post(base + "/collect", json={"start_month": "2026-07"}, headers=self._headers(admin_session["csrf_token"]))
                self.assertEqual(response.status_code, 202, response.text)
                start.assert_called_once()

    def test_disabling_account_invalidates_session_and_hides_account_state(self):
        admin_session = self._setup_admin()
        viewer = self._create_user(admin_session["csrf_token"])
        viewer_client = self._new_client()
        self._login(viewer["username"], VIEWER_PASSWORD, viewer_client)
        self.assertEqual(viewer_client.get("/api/auth/me").status_code, 200)

        disabled = self.client.patch(
            "/api/admin/users/%s" % viewer["id"],
            json={"is_active": False},
            headers=self._headers(admin_session["csrf_token"]),
        )
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertFalse(disabled.json()["user"]["is_active"])
        self.assertEqual(viewer_client.get("/api/auth/me").status_code, 401)

        disabled_login = viewer_client.post(
            "/api/auth/login",
            json={"username": viewer["username"], "password": VIEWER_PASSWORD},
            headers=self._headers(),
        )
        wrong_login = viewer_client.post(
            "/api/auth/login",
            json={"username": "missing.user", "password": VIEWER_PASSWORD},
            headers=self._headers(),
        )
        self.assertEqual(disabled_login.status_code, 401)
        self.assertEqual(wrong_login.status_code, 401)
        self.assertEqual(disabled_login.json()["detail"], wrong_login.json()["detail"])

    def test_deleting_account_revokes_sessions_and_removes_user(self):
        admin_session = self._setup_admin()
        viewer = self._create_user(admin_session["csrf_token"])
        viewer_client = self._new_client()
        self._login(viewer["username"], VIEWER_PASSWORD, viewer_client)

        deleted = self.client.delete(
            "/api/admin/users/%s" % viewer["id"],
            headers=self._headers(admin_session["csrf_token"]),
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["user"]["id"], viewer["id"])
        self.assertNotIn(viewer["id"], {user["id"] for user in self.auth.list_users()})
        self.assertEqual(viewer_client.get("/api/auth/me").status_code, 401)

        delete_self = self.client.delete(
            "/api/admin/users/%s" % admin_session["user"]["id"],
            headers=self._headers(admin_session["csrf_token"]),
        )
        self.assertEqual(delete_self.status_code, 409)

    def test_password_reset_revokes_sessions_and_old_password(self):
        admin_session = self._setup_admin()
        viewer = self._create_user(admin_session["csrf_token"])
        viewer_client = self._new_client()
        self._login(viewer["username"], VIEWER_PASSWORD, viewer_client)

        reset = self.client.post(
            "/api/admin/users/%s/reset-password" % viewer["id"],
            json={"password": RESET_PASSWORD},
            headers=self._headers(admin_session["csrf_token"]),
        )
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertEqual(viewer_client.get("/api/auth/me").status_code, 401)

        old_login = viewer_client.post(
            "/api/auth/login",
            json={"username": viewer["username"], "password": VIEWER_PASSWORD},
            headers=self._headers(),
        )
        self.assertEqual(old_login.status_code, 401)
        self._login(viewer["username"], RESET_PASSWORD, viewer_client)

    def test_permission_implications_and_last_manager_are_protected(self):
        admin_session = self._setup_admin()
        admin = admin_session["user"]

        disable_self = self.client.patch(
            "/api/admin/users/%s" % admin["id"],
            json={"is_active": False},
            headers=self._headers(admin_session["csrf_token"]),
        )
        self.assertEqual(disable_self.status_code, 409)

        remove_management = self.client.patch(
            "/api/admin/users/%s" % admin["id"],
            json={"permissions": ["portal.view"]},
            headers=self._headers(admin_session["csrf_token"]),
        )
        self.assertEqual(remove_management.status_code, 409)

        update_self_position = self.client.patch(
            "/api/admin/users/%s" % admin["id"],
            json={"position": "平台负责人"},
            headers=self._headers(admin_session["csrf_token"]),
        )
        self.assertEqual(update_self_position.status_code, 200, update_self_position.text)
        self.assertEqual(update_self_position.json()["user"]["position"], "平台负责人")

        downloader = self._create_user(
            admin_session["csrf_token"],
            username="report.downloader",
            display_name="报表下载者",
            permissions=["portal.download"],
        )
        self.assertEqual(
            downloader["permissions"], ["portal.view", "portal.download"]
        )

        manager = self._create_user(
            admin_session["csrf_token"],
            username="user.manager",
            display_name="用户管理员",
            permissions=["users.manage"],
        )
        self.assertEqual(
            manager["permissions"],
            ["portal.view", "admin.access", "users.manage"],
        )

        me = self.client.get("/api/auth/me")
        permission_keys = {
            item["key"] for item in me.json()["permission_definitions"]
        }
        self.assertEqual(permission_keys, {
            "portal.view", "portal.download", "boss_report.download",
            "monitoring.view", "payment.view", "payment.manage",
            "admin.access", "users.manage",
        })
        role_templates = {item["key"]: item for item in me.json()["role_templates"]}
        self.assertEqual(set(role_templates), {
            "super_admin", "payment_manager", "finance_viewer",
            "store_manager", "data_viewer",
        })
        self.assertEqual(role_templates["store_manager"]["scope_type"], "venues")
        self.assertEqual(
            role_templates["store_manager"]["permissions"],
            ["portal.view", "portal.download"],
        )
        self.assertIn("payment.manage", role_templates["payment_manager"]["permissions"])

    def test_role_templates_are_persisted_and_enforced_server_side(self):
        admin_session = self._setup_admin()
        payment = self._create_user(
            admin_session["csrf_token"],
            username="role.payment",
            display_name="货款角色",
            permissions=["portal.view"],
            role_key="payment_manager",
        )
        self.assertEqual(payment["role_key"], "payment_manager")
        self.assertEqual(payment["permissions"], [
            "portal.view", "portal.download", "payment.view", "payment.manage",
        ])
        revised = self.client.patch(
            "/api/admin/users/%s" % payment["id"],
            json={
                "role_key": "store_manager",
                "permissions": ["users.manage"],
                "scope_type": "all",
                "venues": ["深圳南山"],
            },
            headers=self._headers(admin_session["csrf_token"]),
        )
        self.assertEqual(revised.status_code, 200, revised.text)
        store_manager = revised.json()["user"]
        self.assertEqual(store_manager["role_key"], "store_manager")
        self.assertEqual(store_manager["permissions"], ["portal.view", "portal.download"])
        self.assertEqual(store_manager["scope_type"], "venues")
        self.assertEqual(store_manager["venues"], ["深圳南山"])

    def test_user_venue_scope_is_normalized_and_admin_is_unrestricted(self):
        admin_session = self._setup_admin()
        store_user = self._create_user(
            admin_session["csrf_token"],
            username="store.owner",
            display_name="门店负责人",
            permissions=["portal.download"],
            position="门店负责人",
            scope_type="venues",
            venues=[" 深圳南山 ", "深圳南山", "", "广州天河"],
        )
        self.assertEqual(store_user["scope_type"], "venues")
        self.assertEqual(store_user["position"], "门店负责人")
        self.assertEqual(store_user["venues"], ["深圳南山", "广州天河"])
        self.assertEqual(
            self.auth.effective_venue_scope(store_user),
            {"深圳南山", "广州天河"},
        )
        store_client = self._new_client()
        store_login = self._login("store.owner", VIEWER_PASSWORD, store_client)
        self.assertEqual(store_login["redirect"], "/store")
        self.assertEqual(store_client.get("/store").status_code, 200)
        revised = self.client.patch(
            "/api/admin/users/%s" % store_user["id"],
            json={"position": "华南区域负责人", "scope_type": "venues", "venues": ["深圳南山"]},
            headers=self._headers(admin_session["csrf_token"]),
        )
        self.assertEqual(revised.status_code, 200, revised.text)
        self.assertEqual(revised.json()["user"]["position"], "华南区域负责人")
        self.assertEqual(store_client.get("/api/auth/me").status_code, 401)

        all_user = self._create_user(
            admin_session["csrf_token"],
            username="company.viewer",
            display_name="公司查看者",
            permissions=["portal.view"],
        )
        self.assertIsNone(self.auth.effective_venue_scope(all_user))
        self.assertIsNone(self.auth.effective_venue_scope(admin_session["user"]))

        invalid = self.client.post(
            "/api/admin/users",
            json={
                "username": "empty.scope",
                "display_name": "空范围",
                "password": VIEWER_PASSWORD,
                "permissions": ["portal.view"],
                "scope_type": "venues",
                "venues": [],
            },
            headers=self._headers(admin_session["csrf_token"]),
        )
        self.assertEqual(invalid.status_code, 422)

    def test_admin_can_maintain_venue_open_and_close_dates(self):
        admin_session = self._setup_admin()
        roster = {
            "source": "dashboard",
            "available": True,
            "rows": [{"venue": "香港H店", "owner": "王五", "operating": True}],
        }
        with mock.patch.object(
            self.app_module.payment_accounting,
            "get_venue_roster",
            return_value=roster,
        ):
            saved = self.client.patch(
                "/api/admin/venues/%E9%A6%99%E6%B8%AFH%E5%BA%97",
                json={"opened_on": "2026-07-01", "closed_on": "2026-08-31"},
                headers=self._headers(admin_session["csrf_token"]),
            )
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(saved.json()["item"]["opened_on"], "2026-07-01")
            self.assertEqual(saved.json()["item"]["closed_on"], "2026-08-31")
            self.assertFalse(saved.json()["item"]["operating"])

            listed = self.client.get("/api/admin/venues")
            self.assertEqual(listed.status_code, 200, listed.text)
            item = next(row for row in listed.json()["items"] if row["venue"] == "香港H店")
            self.assertTrue(item["lifecycle_configured"])

            invalid = self.client.patch(
                "/api/admin/venues/%E9%A6%99%E6%B8%AFH%E5%BA%97",
                json={"opened_on": "2026-09-01", "closed_on": "2026-08-31"},
                headers=self._headers(admin_session["csrf_token"]),
            )
            self.assertEqual(invalid.status_code, 422)

        viewer = self._create_user(admin_session["csrf_token"])
        viewer_client = self._new_client()
        self._login(viewer["username"], VIEWER_PASSWORD, viewer_client)
        self.assertEqual(viewer_client.get("/api/admin/venues").status_code, 403)

    def test_route_catalog_has_no_auth_or_legacy_bypass(self):
        module = self.app_module
        registered = set()
        duplicates = set()
        for route in self.app.routes:
            path = getattr(route, "path", "")
            for method in getattr(route, "methods", None) or set():
                key = (method, path)
                if key in registered:
                    duplicates.add(key)
                registered.add(key)
        self.assertFalse(duplicates, "重复 method/path: %r" % sorted(duplicates))

        required_routes = {
            ("POST", "/api/auth/setup"),
            ("POST", "/api/auth/login"),
            ("GET", "/api/auth/me"),
            ("POST", "/api/auth/logout"),
            ("GET", "/api/admin/users"),
            ("GET", "/api/admin/venues"),
            ("PATCH", "/api/admin/venues/{venue}"),
            ("POST", "/api/admin/users"),
            ("PATCH", "/api/admin/users/{user_id}"),
            ("DELETE", "/api/admin/users/{user_id}"),
            ("POST", "/api/admin/users/{user_id}/reset-password"),
        }
        self.assertTrue(required_routes <= registered)
        self.assertEqual(module._PUBLIC_AUTH_PATHS, {
            "/api/auth/setup",
            "/api/auth/login",
        })

        specialized = {
            **{route: "boss_report.download" for route in module.BOSS_REPORT_PERMISSION_ROUTES},
            **{route: "payment.view" for route in module.PAYMENT_VIEW_PERMISSION_ROUTES},
            **{route: "payment.manage" for route in module.PAYMENT_MANAGE_PERMISSION_ROUTES},
            **{route: "monitoring.view" for route in module.MONITORING_VIEW_PERMISSION_ROUTES},
        }
        for catalog, prefix, permission in (
            (module.PORTAL_API_ROUTES, module.PORTAL_API_PREFIX, "portal.view"),
            (module.ADMIN_API_ROUTES, module.ADMIN_API_PREFIX, "admin.access"),
        ):
            for method, legacy_path in catalog:
                canonical_path = prefix + legacy_path[len("/api"):]
                with self.subTest(method=method, legacy=legacy_path):
                    self.assertIn((method, legacy_path), registered)
                    self.assertIn((method, canonical_path), registered)
                    expected_permission = specialized.get((method, legacy_path), permission)
                    if legacy_path.startswith("/api/download/"):
                        expected_permission = "portal.download"
                    self.assertEqual(
                        module._required_permission(method, legacy_path),
                        expected_permission,
                    )
                    self.assertEqual(
                        module._required_permission(method, canonical_path),
                        expected_permission,
                    )

        self.assertTrue(all(method == "GET" for method, _ in module.PORTAL_API_ROUTES))
        self.assertEqual(
            module._required_permission("GET", "/api/not-catalogued"),
            "admin.access",
        )
        self.assertEqual(
            module._required_permission("GET", "/api/admin/users"),
            "users.manage",
        )


    def test_slow_dashboard_does_not_block_auth_probe(self):
        import httpx

        self._setup_admin()
        entered, release = threading.Event(), threading.Event()
        calls = []

        def slow_dashboard(date):
            calls.append(("dashboard", threading.get_ident()))
            entered.set()
            release.wait(3)
            return {"ready": False}

        def tracked(name, original):
            def call(*args, **kwargs):
                calls.append((name, threading.get_ident()))
                return original(*args, **kwargs)
            return call

        async def check():
            loop_thread = threading.get_ident()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=BASE_ORIGIN, cookies=self.client.cookies) as client:
                slow = asyncio.create_task(client.get("/api/portal/dashboard/data?date=2026-08-30"))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    self.assertTrue(all(tid != loop_thread for _, tid in calls), calls)
                    probe = await asyncio.wait_for(client.get("/api/auth/me"), timeout=2)
                    self.assertEqual(probe.status_code, 200)
                    self.assertFalse(slow.done(), "slow query must still be waiting during the auth probe")
                finally:
                    release.set()
                    result = await slow
                self.assertEqual(result.status_code, 200)

        with mock.patch.object(self.app_module, "get_dashboard_data", side_effect=slow_dashboard), \
                mock.patch.object(self.auth, "user_count", side_effect=tracked("user_count", self.auth.user_count)), \
                mock.patch.object(self.auth, "get_session", side_effect=tracked("get_session", self.auth.get_session)):
            asyncio.run(check())
        self.assertEqual({name for name, _ in calls}, {"dashboard", "user_count", "get_session"})

    def test_payment_upload_history_and_limits(self):
        import io
        session = self._setup_admin()
        headers = self._headers(session["csrf_token"])
        workbook = io.BytesIO()
        self.app_module.pd.DataFrame({"店名": ["A"], "金额": [100]}).to_excel(workbook, index=False)
        payload = {"date": "2026-09-01"}
        files = {"file": ("test.xlsx", workbook.getvalue())}
        response = self.client.post("/api/payment-data/upload", data=payload, files=files, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        history = self.client.get("/api/admin/payment-data/history/2026-09-01").json()["history"]
        self.assertEqual(history[0]["actor_user_id"], session["user"]["id"])
        with mock.patch.object(self.app_module.payment_upload, "MAX_ROWS", 0):
            response = self.client.post("/api/payment-data/upload", data=payload, files=files, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.app_module.payment_store.get_date_info("2026-09-01")["total"], 100)
        self.assertEqual(len(self.app_module.payment_store.list_history("2026-09-01")), len(history))
        self.auth.create_user("scope.history", "Scoped", VIEWER_PASSWORD,
                              ["payment.view"], scope_type="venues", venues=["A"])
        self._login("scope.history", VIEWER_PASSWORD)
        self.assertEqual(self.client.get("/api/payment-data/history/2026-09-01").status_code, 403)

    def test_upload_parse_and_save_use_worker_thread(self):
        import io
        import httpx
        import pandas as pd

        session = self._setup_admin()
        calls = []
        workbook = io.BytesIO()
        pd.DataFrame({"店名": ["test-shop"], "金额": [100]}).to_excel(workbook, index=False)

        def parse(*args, **kwargs):
            calls.append(threading.get_ident())
            return pd.DataFrame({"店名": ["test-shop"], "金额": [100]})

        def save(*args, **kwargs):
            calls.append(threading.get_ident())
            return 1

        async def check():
            loop_thread = threading.get_ident()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=BASE_ORIGIN, cookies=self.client.cookies) as client:
                unsupported = await client.post("/api/payment-data/upload", data={"date": "2026-08-30"}, files={"file": ("test.xls", b"fake-excel")}, headers=self._headers(session["csrf_token"]))
                self.assertEqual(unsupported.status_code, 400)
                result = await client.post("/api/payment-data/upload", data={"date": "2026-08-30"}, files={"file": ("test.xlsx", workbook.getvalue())}, headers=self._headers(session["csrf_token"]))
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["total"], 100)
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(tid != loop_thread for tid in calls))

        with mock.patch.object(self.app_module.pd, "read_excel", side_effect=parse), \
                mock.patch.object(self.app_module.payment_store, "replace_date_rows", side_effect=save):
            asyncio.run(check())

    def test_login_password_check_and_session_write_use_worker_thread(self):
        import httpx

        self._setup_admin()
        calls = []

        def tracked(original):
            def call(*args, **kwargs):
                calls.append(threading.get_ident())
                return original(*args, **kwargs)
            return call

        async def check():
            loop_thread = threading.get_ident()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=BASE_ORIGIN) as client:
                result = await client.post("/api/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}, headers=self._headers())
            self.assertEqual(result.status_code, 200)
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(tid != loop_thread for tid in calls))

        with mock.patch.object(self.auth, "authenticate", side_effect=tracked(self.auth.authenticate)), \
                mock.patch.object(self.auth, "create_session", side_effect=tracked(self.auth.create_session)):
            asyncio.run(check())

    def test_blocking_endpoints_use_fastapi_worker_dispatch(self):
        for name in (
            "login_page", "auth_setup", "auth_logout", "admin_list_users", "admin_create_user",
            "admin_update_user", "admin_delete_user", "admin_reset_user_password", "get_tasks", "start_collect",
            "start_single_collect", "stop_task", "get_summary", "download_summary", "boss_report",
            "boss_report_check", "payment_data_years", "payment_data_month", "payment_data_date",
            "payment_data_upload", "payment_data_delete", "payment_accounting_entry_list",
            "payment_accounting_entry_save", "payment_accounting_month", "payment_accounting_summary",
            "dashboard_status", "collection_status", "dashboard_data", "daily_operations",
            "monitoring_overview", "monitoring_sync_status", "monitoring_sync", "data_quality",
            "data_quality_collect_missing", "get_logs", "get_credentials", "save_credential", "get_credential",
        ):
            with self.subTest(endpoint=name):
                endpoint = getattr(self.app_module, name)
                self.assertFalse(inspect.iscoroutinefunction(endpoint))
                routes = [route for route in self.app.routes if getattr(route, "endpoint", None) is endpoint]
                self.assertTrue(routes)

    def test_auto_collection_can_be_disabled_at_startup(self):
        with mock.patch.dict(os.environ, {"WORKBUDDY_DISABLE_AUTO_COLLECTION": "1"}), \
                mock.patch.object(self.app_module.monitor_auto_scheduler, "start") as monitor_start, \
                mock.patch.object(self.app_module.collection_auto_scheduler, "start") as collection_start:
            self.app_module.start_monitor_auto_scheduler()
            self.app_module.start_collection_auto_scheduler()
            monitor_start.assert_not_called()
            collection_start.assert_not_called()

    def test_lifespan_starts_and_stops_background_services_in_order(self):
        module = self.app_module
        calls = []
        replacements = {
            "start_backup_scheduler": lambda: calls.append("start_backup"),
            "start_alert_dispatcher": lambda: calls.append("start_alerts"),
            "start_monitor_auto_scheduler": lambda: calls.append("start_monitor"),
            "start_collection_auto_scheduler": lambda: calls.append("start_collection"),
            "stop_collection_auto_scheduler": lambda: calls.append("stop_collection"),
            "stop_monitor_auto_scheduler": lambda: calls.append("stop_monitor"),
            "stop_alert_dispatcher": lambda: calls.append("stop_alerts"),
            "stop_backup_scheduler": lambda: calls.append("stop_backup"),
        }

        async def exercise():
            with mock.patch.multiple(module, **replacements):
                async with module.app_lifespan(module.app):
                    calls.append("serving")

        asyncio.run(exercise())
        self.assertEqual(
            calls,
            [
                "start_backup", "start_alerts", "start_monitor", "start_collection",
                "serving",
                "stop_collection", "stop_monitor", "stop_alerts", "stop_backup",
            ],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2, warnings="ignore")
