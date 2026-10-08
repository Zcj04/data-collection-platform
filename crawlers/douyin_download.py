# -*- coding: utf-8 -*-
"""抖音来客收入日报下载"""

import json
import os
import random
import time
import uuid
from pathlib import Path

import requests
from utils.http import create_retry_session
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

LOGIN_URL = "https://life.douyin.com/p/login"
CLASSIFY_URL = "https://life.douyin.com/life/settle/v2/daily_income/classify/"

# Cookie 目录：项目根/credentials/douyin/
_BASE = Path(__file__).resolve().parent.parent
_DOUYIN_DIR = _BASE / "credentials" / "douyin"
_DOUYIN_DIR.mkdir(parents=True, exist_ok=True)

PROFILE_DIR = _DOUYIN_DIR / "browser_profile"
STATE_FILE = _DOUYIN_DIR / "storage_state.json"
COOKIE_JSON_FILE = _DOUYIN_DIR / "cookies.json"
COOKIE_HEADER_FILE = _DOUYIN_DIR / "cookie_header.txt"

AUTO_LOGIN_TIMEOUT = 90

ROOT_LIFE_ACCOUNT_IDS = tuple(
    i.strip() for i in os.getenv("DOUYIN_ROOT_ACCOUNT_IDS", "").split(",") if i.strip()
)


class NeedLogin(RuntimeError):
    pass


class LoginBlocked(RuntimeError):
    pass


def parse_cookie_header(cookie_header):
    cookie_map = {}
    for part in str(cookie_header or "").split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookie_map[name.strip()] = value.strip()
    return cookie_map


def cookie_list_to_header(cookies):
    useful = []
    for c in cookies:
        domain = c.get("domain", "")
        name = c.get("name", "")
        value = c.get("value", "")
        if name and ("douyin.com" in domain or "bytedance.com" in domain):
            useful.append(f"{name}={value}")
    return "; ".join(useful)


def has_login_cookie_from_list(cookies):
    names = {c.get("name") for c in cookies}
    return bool({"sessionid_ls", "sessionid_ss_ls", "sid_guard_ls", "uid_tt_ls", "uid_tt_ss_ls"} & names)


def save_login_state(context):
    context.storage_state(path=str(STATE_FILE))
    cookies = context.cookies()
    COOKIE_JSON_FILE.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    COOKIE_HEADER_FILE.write_text(cookie_list_to_header(cookies), encoding="utf-8")


def load_saved_auth():
    if not COOKIE_HEADER_FILE.exists():
        raise NeedLogin("没有已保存的抖音来客登录态")
    cookie_header = COOKIE_HEADER_FILE.read_text(encoding="utf-8").strip()
    if not cookie_header:
        raise NeedLogin("已保存 Cookie 为空")
    cookies = json.loads(COOKIE_JSON_FILE.read_text(encoding="utf-8"))
    cookie_map = {c["name"]: c["value"] for c in cookies if c.get("name") and c.get("value")}
    cookie_map.update(parse_cookie_header(cookie_header))
    return cookie_header, cookie_map


def make_trace_id():
    return f"00-{uuid.uuid4().hex}-{random.getrandbits(64):016x}-01"


def build_headers(cookie_header, cookie_map):
    csrf_token = cookie_map.get("csrf_session_id", "")
    ls_session_id = cookie_map.get("sessionid_ls", "")
    return {
        "x-secsdk-csrf-token": csrf_token,
        "x-tt-ls-session-id": ls_session_id,
        "x-tt-trace-id": make_trace_id(),
        "x-tt-trace-log": "01",
        "Cookie": cookie_header,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
        "content-type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://life.douyin.com",
        "Referer": "https://life.douyin.com/",
        "Host": "life.douyin.com",
    }


def get_auth_headers():
    cookie_header, cookie_map = load_saved_auth()
    return build_headers(cookie_header, cookie_map)


def auto_login(account, password):
    if not account or not password:
        raise RuntimeError("缺少抖音账号或密码")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1400, "height": 900},
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)

        if has_login_cookie_from_list(context.cookies()):
            save_login_state(context)
            context.close()
            return

        try:
            page.get_by_text("立即登录").last.click(timeout=8000)
            page.wait_for_timeout(1000)
        except PlaywrightTimeoutError:
            pass

        # 密码登录
        for _ in range(3):
            try:
                el = page.get_by_text("密码登录", exact=True).first
                if el.is_visible(timeout=1000):
                    el.click(timeout=5000)
                    break
            except Exception:
                pass
            page.wait_for_timeout(1000)

        page.wait_for_timeout(800)
        phone = page.locator('input[placeholder="手机号码"]').first
        pwd = page.locator('input[placeholder="密码"]').first
        phone.fill(account)
        pwd.fill(password)

        # 勾选协议
        page.evaluate("()=>{document.querySelectorAll('input[type=\"checkbox\"]').forEach(c=>{c.checked=true;c.dispatchEvent(new Event('change',{bubbles:true}))})}")
        page.wait_for_timeout(500)

        # 点登录
        page.locator("button").filter(has_text="登录").first.click(timeout=8000)
        page.wait_for_timeout(2000)

        deadline = time.time() + AUTO_LOGIN_TIMEOUT
        while time.time() < deadline:
            page.wait_for_timeout(1000)
            cookies = context.cookies()
            if has_login_cookie_from_list(cookies):
                save_login_state(context)
                context.close()
                return
        context.close()
        raise TimeoutError("自动登录超时")


def build_payload(start_date, end_date):
    return {
        "filter": {"start_date": start_date, "end_date": end_date, "daily_income_dim_type": 2, "goods_id_list": [], "show_empty": False},
        "permission_common_param": {"all_selected_params": json.dumps({"ExpandToPoiAccount": True, "RelationTypes": [1, 2, 3, 5]}, ensure_ascii=False)},
        "biz_type": 1,
    }


def post_json(session, headers, payload, root_life_account_id, page_index):
    h = dict(headers)
    h["x-tt-trace-id"] = make_trace_id()
    resp = session.post(CLASSIFY_URL, headers=h, params={
        "page_index": page_index, "page_size": 10,
        "root_life_account_id": root_life_account_id,
    }, json=payload, timeout=30)
    if resp.status_code in (401, 403):
        raise NeedLogin("登录态失效")
    resp.raise_for_status()
    return resp.json()


def fetch_all_accounts(start_date, end_date, headers):
    payload = build_payload(start_date, end_date)
    session = create_retry_session()
    session.trust_env = False
    try:
        rows = []
        for aid in ROOT_LIFE_ACCOUNT_IDS:
            pi = 1
            while True:
                data = post_json(session, headers, payload, aid, pi)
                data_list = data.get("data", {}).get("list", [])
                if not data_list:
                    break
                for item in data_list:
                    amt = item.get("order_actual_receive_amount", 0) or 0
                    fee = item.get("service_fee", 0) or 0
                    rows.append([item.get("classify_name", ""), amt / 100, fee / 100, (amt - fee) / 100])
                pi += 1
        return rows
    finally:
        session.close()


def merge_rows(rows):
    merged = {}
    for row in rows:
        name = row[0]
        if name in merged:
            merged[name][1] += row[1]
            merged[name][2] += row[2]
            merged[name][3] += row[3]
        else:
            merged[name] = row.copy()
    return [{"抖音店铺名": r[0], "抖音收款": round(r[1], 2), "抖音手续费": round(r[2], 2), "抖音实收": round(r[3], 2)} for r in merged.values()]


def main(start_date, end_date, account=None, password=None):
    try:
        headers = get_auth_headers()
        rows = fetch_all_accounts(start_date, end_date, headers)
    except NeedLogin:
        auto_login(
            account or os.getenv("DOUYIN_ACCOUNT"),
            password or os.getenv("DOUYIN_PASSWORD"),
        )
        headers = get_auth_headers()
        rows = fetch_all_accounts(start_date, end_date, headers)
    return merge_rows(rows)
