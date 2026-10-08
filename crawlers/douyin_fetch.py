# -*- coding: utf-8 -*-
"""
抖音来客收入采集（原八爪鱼脚本整理，Playwright 自动登录版）

主入口：main(start_date, end_date) -> List[Dict]
    输入：起止日期 YYYY-MM-DD
    输出：[{"抖音店铺名": str, "抖音收款": float, "抖音手续费": float, "抖音实收": float}, ...]

流程：
  1. 尝试读取本地 Cookie（STATE_FILE/COOKIE_JSON_FILE）
  2. Cookie 有效 → 直接调用 API 拉取收入分类数据（分页，2个 ROOT_LIFE_ACCOUNT_IDS）
  3. Cookie 失效 → Playwright 自动打开浏览器登录（填手机号/密码/勾协议/点登录）
     - 若触滑块/验证码/风控 → 抛 LoginBlocked，需人工去抖音来客官网验证一次
  4. 登录成功后保存 Cookie 供后续使用
  5. 合并多账号数据 → 返回店铺维度结果

注意：
- 账号密码已支持环境变量 DOUYIN_LIFE_ACCOUNT / DOUYIN_LIFE_PASSWORD（默认值在代码里）
- Playwright 需要 headless=False（用户可见浏览器），需安装：pip install playwright && playwright install chromium
- 核心业务逻辑保持原样，未做功能性改动
"""

import argparse
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


# =========================================================
# 1. 基础配置
# =========================================================

LOGIN_URL = "https://life.douyin.com/p/login"
CLASSIFY_URL = "https://life.douyin.com/life/settle/v2/daily_income/classify/"

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "douyin_life_browser_profile"
STATE_FILE = BASE_DIR / "douyin_life_storage_state.json"
COOKIE_JSON_FILE = BASE_DIR / "douyin_life_cookies.json"
COOKIE_HEADER_FILE = BASE_DIR / "douyin_life_cookie_header.txt"

# 账号密码只从环境变量/凭证管理读取，禁止硬编码默认值
LOGIN_ACCOUNT = os.getenv("DOUYIN_LIFE_ACCOUNT")
LOGIN_PASSWORD = os.getenv("DOUYIN_LIFE_PASSWORD")

AUTO_LOGIN_TIMEOUT_SECONDS = 90

ROOT_LIFE_ACCOUNT_IDS = tuple(
    i.strip() for i in os.getenv("DOUYIN_ROOT_ACCOUNT_IDS", "").split(",") if i.strip()
)


class NeedLogin(RuntimeError):
    """保存的登录态缺失、过期或接口要求重新登录。"""


class LoginBlocked(RuntimeError):
    """自动登录被验证码、短信、滑块或风控拦住。"""


# =========================================================
# 2. Cookie / Header
# =========================================================

def parse_cookie_header(cookie_header):
    cookie_map = {}
    for part in str(cookie_header or "").split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if name:
            cookie_map[name] = value
    return cookie_map


def cookie_list_to_header(cookies):
    useful = []
    for cookie in cookies:
        domain = cookie.get("domain", "")
        name = cookie.get("name", "")
        value = cookie.get("value", "")
        if not name:
            continue
        if (
            "douyin.com" in domain
            or "bytedance.com" in domain
            or "oceanengine.com" in domain
        ):
            useful.append(f"{name}={value}")
    return "; ".join(useful)


def has_login_cookie_from_list(cookies):
    names = {cookie.get("name") for cookie in cookies}
    return bool(
        {
            "sessionid_ls",
            "sessionid_ss_ls",
            "sid_guard_ls",
            "uid_tt_ls",
            "uid_tt_ss_ls",
        }
        & names
    )


def save_login_state(context):
    context.storage_state(path=str(STATE_FILE))
    cookies = context.cookies()

    COOKIE_JSON_FILE.write_text(
        json.dumps(cookies, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    COOKIE_HEADER_FILE.write_text(
        cookie_list_to_header(cookies),
        encoding="utf-8",
    )

    print("抖音登录态已自动保存")


def load_saved_auth():
    if not COOKIE_HEADER_FILE.exists() or not COOKIE_JSON_FILE.exists():
        raise NeedLogin("没有找到已保存的抖音来客登录态")

    cookie_header = COOKIE_HEADER_FILE.read_text(encoding="utf-8").strip()
    if not cookie_header:
        raise NeedLogin("已保存 Cookie 为空")

    cookies = json.loads(COOKIE_JSON_FILE.read_text(encoding="utf-8"))
    cookie_map = {}
    for item in cookies:
        name = item.get("name")
        value = item.get("value")
        if name and value:
            cookie_map[name] = value

    cookie_map.update(parse_cookie_header(cookie_header))
    return cookie_header, cookie_map


def make_trace_id():
    return f"00-{uuid.uuid4().hex}-{random.getrandbits(64):016x}-01"


def build_headers(cookie_header, cookie_map):
    csrf_token = (
        cookie_map.get("csrf_session_id")
        or cookie_map.get("passport_csrf_token")
        or cookie_map.get("passport_csrf_token_default")
        or ""
    )
    ls_session_id = (
        cookie_map.get("sessionid_ls")
        or cookie_map.get("sessionid_ss_ls")
        or cookie_map.get("sid_guard_ls")
        or ""
    )

    return {
        "ac-tag": "smb_l",
        "agw-js-conv": "str",
        "priority": "u=1, i",
        "rpc-persist-life-biz-view-id": "0",
        "rpc-persist-life-merchant-role": "1332661909",
        "rpc-persist-life-merchant-switch-role": "1",
        "rpc-persist-life-platform": "pc",
        "rpc-persist-lite-app-id": "100277",
        "rpc-persist-terminal-type": "1",
        "x-secsdk-csrf-token": csrf_token,
        "x-tt-ls-session-id": ls_session_id,
        "x-tt-trace-id": make_trace_id(),
        "x-tt-trace-log": "01",
        "Cookie": cookie_header,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "content-type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://life.douyin.com",
        "Referer": "https://life.douyin.com/",
        "Host": "life.douyin.com",
        "Connection": "keep-alive",
    }


def get_auth_headers():
    cookie_header, cookie_map = load_saved_auth()
    return build_headers(cookie_header, cookie_map)


# =========================================================
# 3. 强制自动登录
# =========================================================

def first_visible(locator):
    try:
        count = locator.count()
    except Exception:
        return None

    for index in range(count):
        item = locator.nth(index)
        try:
            if item.is_visible(timeout=300):
                return item
        except Exception:
            pass
    return None


def click_if_visible(locator, timeout=3_000):
    try:
        item = first_visible(locator)
        if item:
            item.click(timeout=timeout)
            return True
    except Exception:
        return False
    return False


def body_text(page):
    try:
        return page.locator("body").inner_text(timeout=1_000)
    except Exception:
        return ""


def page_has_blocking_verification(page):
    text = body_text(page)
    keywords = (
        "拖动滑块",
        "请拖动",
        "滑块验证",
        "短信验证",
        "安全验证",
        "身份验证",
        "风险",
        "频繁",
        "请输入验证码",
    )
    return any(keyword in text for keyword in keywords)


def force_check_agreement(page):
    """
    抖音来客的协议勾选框有时不是普通可见 checkbox。
    这里用多种方式强制触发：
      1. 点真实 input[type=checkbox]
      2. 点"已阅读并同意"文字左侧的视觉勾选框
      3. JS 触发 click/input/change
    """
    clicked = False

    checkbox_locator = page.locator('input[type="checkbox"]')
    try:
        for index in range(checkbox_locator.count()):
            checkbox = checkbox_locator.nth(index)
            try:
                if checkbox.is_visible(timeout=300):
                    if not checkbox.is_checked():
                        checkbox.click(force=True, timeout=2_000)
                    clicked = True
            except Exception:
                pass
    except Exception:
        pass

    for text in ("已阅读并同意", "用户协议", "隐私条款"):
        try:
            item = first_visible(page.get_by_text(text))
            if not item:
                continue

            box = item.bounding_box()
            if box:
                # 协议文字左边通常就是视觉 checkbox。
                page.mouse.click(
                    max(1, box["x"] - 18),
                    box["y"] + box["height"] / 2,
                )
                page.wait_for_timeout(300)
                clicked = True

            item.click(force=True, timeout=2_000)
            page.wait_for_timeout(300)
            clicked = True
            break
        except Exception:
            pass

    try:
        page.evaluate(
            """
            () => {
                const inputs = Array.from(
                    document.querySelectorAll('input[type="checkbox"]')
                );
                for (const input of inputs) {
                    try {
                        if (!input.checked) input.click();
                        input.checked = true;
                        input.dispatchEvent(new Event('input', {bubbles: true}));
                        input.dispatchEvent(new Event('change', {bubbles: true}));
                    } catch (e) {}
                }

                const all = Array.from(document.querySelectorAll('*'));
                const agreement = all.find(el => {
                    const text = (el.innerText || el.textContent || '').trim();
                    return text.includes('已阅读并同意');
                });
                if (agreement) {
                    const rect = agreement.getBoundingClientRect();
                    const x = Math.max(1, rect.left - 18);
                    const y = rect.top + rect.height / 2;
                    const target = document.elementFromPoint(x, y);
                    if (target) {
                        target.dispatchEvent(
                            new MouseEvent('click', {
                                bubbles: true,
                                cancelable: true,
                                view: window,
                                clientX: x,
                                clientY: y
                            })
                        );
                    }
                }
            }
            """
        )
        clicked = True
    except Exception:
        pass

    if not clicked:
        raise RuntimeError("自动登录失败：没有找到协议复选框")


def click_login_button(page):
    # 优先点真正 button，避免误点"验证码登录 / 密码登录"切换文字。
    button_locator = page.locator("button")
    try:
        for index in range(button_locator.count()):
            button = button_locator.nth(index)
            try:
                if not button.is_visible(timeout=300):
                    continue
                text = (button.inner_text(timeout=500) or "").strip()
                if text == "登录":
                    button.click(timeout=8_000)
                    return
            except Exception:
                pass
    except Exception:
        pass

    # 兜底：找精确文字"登录"，但排除"密码登录/验证码登录"。
    text_locator = page.get_by_text("登录", exact=True)
    try:
        for index in range(text_locator.count()):
            item = text_locator.nth(index)
            try:
                if item.is_visible(timeout=300):
                    item.click(timeout=8_000)
                    return
            except Exception:
                pass
    except Exception:
        pass

    raise RuntimeError("自动登录失败：找不到登录按钮")


def page_requires_agreement(page):
    text = body_text(page)
    keywords = (
        "请先勾选",
        "请勾选",
        "请阅读并同意",
        "请同意",
        "阅读并同意",
        "用户协议和隐私条款",
    )
    return any(keyword in text for keyword in keywords)


def auto_login():
    """
    强制自动登录：
      1. 打开抖音来客登录页
      2. 自动点"立即登录 / 密码登录"
      3. 自动填手机号、密码、勾协议、点登录
      4. 自动等待登录 cookie 出现
      5. 自动保存 cookie

    如果平台触发滑块、短信、图形验证码或风控，本函数会直接抛错。
    这类安全验证不能也不应该被脚本绕过。
    """
    if not LOGIN_ACCOUNT or not LOGIN_PASSWORD:
        raise RuntimeError("缺少 LOGIN_ACCOUNT 或 LOGIN_PASSWORD")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1400, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
        )

        page = context.pages[0] if context.pages else context.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(2_000)

        if has_login_cookie_from_list(context.cookies()):
            save_login_state(context)
            context.close()
            return

        # 首页入口："已有账号？立即登录"
        try:
            page.get_by_text("立即登录").last.click(timeout=8_000)
            page.wait_for_timeout(1_000)
        except PlaywrightTimeoutError:
            pass

        # 切换到密码登录
        click_if_visible(page.get_by_text("密码登录", exact=True), timeout=5_000)
        page.wait_for_timeout(800)

        phone_input = first_visible(page.locator('input[placeholder="手机号码"]'))
        password_input = first_visible(page.locator('input[placeholder="密码"]'))

        if not phone_input or not password_input:
            context.close()
            raise RuntimeError("自动登录失败：找不到手机号或密码输入框")

        phone_input.fill(LOGIN_ACCOUNT)
        password_input.fill(LOGIN_PASSWORD)

        force_check_agreement(page)
        click_login_button(page)
        page.wait_for_timeout(1_000)

        if page_requires_agreement(page):
            force_check_agreement(page)
            click_login_button(page)

        deadline = time.time() + AUTO_LOGIN_TIMEOUT_SECONDS

        while time.time() < deadline:
            page.wait_for_timeout(1_000)

            if page_has_blocking_verification(page):
                context.close()
                raise LoginBlocked(
                    "抖音触发了滑块/验证码/短信/风控验证，脚本不能绕过这类安全验证。"
                )

            cookies = context.cookies()
            if has_login_cookie_from_list(cookies):
                save_login_state(context)
                context.close()
                return

        context.close()
        raise TimeoutError("自动登录超时：没有等到有效登录 cookie")


# =========================================================
# 4. 抖音收入接口
# =========================================================

def build_payload(start_date, end_date):
    return {
        "filter": {
            "start_date": start_date,
            "end_date": end_date,
            "daily_income_dim_type": 2,
            "goods_id_list": [],
            "show_empty": False,
        },
        "permission_common_param": {
            "all_selected_params": json.dumps(
                {
                    "SearchAllAccountPoiType": 0,
                    "ExpandToPoiAccount": True,
                    "SearchAllAccountPoiStatus": 0,
                    "RelationTypes": [1, 2, 3, 5],
                    "SettleStatusBeforeClaim": [],
                    "Selections": [],
                    "TagIDList": [],
                    "MainCategoryList": {},
                    "SubCategoryList": {},
                    "PermissionKeyList": [],
                    "StoreBizTagList": [],
                    "SxtSolutionStatusList": [],
                    "SxtSolutionPunishStatusList": [],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        },
        "biz_type": 1,
    }


def response_needs_login(response, data=None):
    if response.status_code in (401, 403):
        return True

    text = response.text[:1200].lower()
    login_keywords = (
        "login",
        "passport",
        "csrf",
        "forbidden",
        "unauthorized",
        "未登录",
        "请登录",
        "登录已过期",
        "登录态",
        "鉴权",
        "权限",
    )
    if any(keyword in text for keyword in login_keywords):
        if data is None or not data.get("data"):
            return True

    if isinstance(data, dict):
        code = str(data.get("code", ""))
        msg = str(data.get("msg") or data.get("message") or "")
        if code not in ("0", "", "200") and any(
            keyword in msg for keyword in ("登录", "权限", "csrf", "鉴权")
        ):
            return True

    return False


def post_json_with_login_check(session, headers, payload, params):
    headers = dict(headers)
    headers["x-tt-trace-id"] = make_trace_id()

    response = session.post(
        CLASSIFY_URL,
        headers=headers,
        params=params,
        json=payload,
        timeout=20,
    )

    try:
        data = response.json()
    except ValueError:
        data = None

    if response_needs_login(response, data):
        raise NeedLogin("保存的登录态已失效，或接口要求重新登录")

    response.raise_for_status()

    if not isinstance(data, dict):
        raise RuntimeError(f"接口没有返回 JSON：{response.text[:300]}")

    return data


def fetch_account(session, headers, payload, root_life_account_id):
    result = []
    page_index = 1
    page_size = 10

    while True:
        params = {
            "page_index": page_index,
            "page_size": page_size,
            "sort_key": "",
            "is_asc": "false",
            "root_life_account_id": root_life_account_id,
        }

        data = post_json_with_login_check(session, headers, payload, params)
        data_list = data.get("data", {}).get("list", [])

        print(f"账号 {root_life_account_id} 第 {page_index} 页，数据条数：{len(data_list)}")

        if not data_list:
            break

        for item in data_list:
            order_amount = item.get("order_actual_receive_amount", 0) or 0
            service_fee = item.get("service_fee", 0) or 0

            result.append(
                [
                    item.get("classify_name", ""),
                    order_amount / 100,
                    service_fee / 100,
                    (order_amount - service_fee) / 100,
                ]
            )

        page_index += 1

    return result


def fetch_all_accounts(start_date, end_date, headers):
    payload = build_payload(start_date, end_date)
    session = create_retry_session()
    session.trust_env = False

    rows = []
    for account_id in ROOT_LIFE_ACCOUNT_IDS:
        account_rows = fetch_account(session, headers, payload, account_id)
        # 仅记录数量，避免把接口原始记录（可能含用户信息）写入日志。
        print(f"账号 {account_id} 获取 {len(account_rows)} 条记录")
        rows.extend(account_rows)

    session.close()
    return rows


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

    result = []
    for row in merged.values():
        result.append(
            {
                "抖音店铺名": row[0],
                "抖音收款": round(row[1], 2),
                "抖音手续费": round(row[2], 2),
                "抖音实收": round(row[3], 2),
            }
        )

    return result


# =========================================================
# 5. 主函数
# =========================================================

def main(start_date, end_date):
    try:
        headers = get_auth_headers()
        rows = fetch_all_accounts(start_date, end_date, headers)
    except NeedLogin as exc:
        print(f"本地登录态不可用：{exc}")
        print("开始自动打开网页重新登录...")
        auto_login()
        headers = get_auth_headers()
        rows = fetch_all_accounts(start_date, end_date, headers)

    result = merge_rows(rows)
    print(result)
    return result


def parse_args():
    parser = argparse.ArgumentParser(description="抖音来客收入日报：强制自动登录版")
    parser.add_argument("start_date", help="开始日期，例如 2026-07-01")
    parser.add_argument("end_date", help="结束日期，例如 2026-07-07")
    return parser.parse_args()


if __name__ == "__main__":
    # 单独运行测试：python crawlers/douyin_fetch.py 2026-08-01 2026-08-06
    # 账号密码从环境变量读取（DOUYIN_LIFE_ACCOUNT / DOUYIN_LIFE_PASSWORD）
    args = parse_args()
    data = main(args.start_date, args.end_date)
    print(f"\n共返回 {len(data)} 条店铺数据")
