# -*- coding: utf-8 -*-
"""
油菜花（youcaihua）数据采集

主入口：main(start_date, end_date, username, password) -> List[Dict]
    输入：起止日期 YYYY-MM-DD + 油菜花账号密码（参数传入或环境变量）
    输出：[{"场地": str, "油菜花现金":..., "油菜花微信":..., "油菜花支付宝":...,
           "油菜花盈客宝":...}, ...]（场地维度，4个指标，口径 2026-09-24 与用户确认）

流程：登录(明文Base64密码, 会话Cookie)→获取商场名→按账期拉收银汇总(操作员×支付方式)
    →MySQL场地映射(youcaihua列=商场名)→聚合返回

接口（前端逆向确认，详见 docs/新增平台_油菜花_某省会城市_需求与技术方案.md）：
- POST /DTOWebLogin?username=&password=<Base64>&ValidCode=   密码为明文 Base64 编码
- POST /GetLoginValidCode    登录页引导接口，返回商场名 mallName / 商场编码 mallCode
- POST /Finance/DTOGetCashSumByEmp   JSON {"StartPeriod","EndPeriod","DateType":99}
    返回 Data.rows=[{EmpName, PayDetails:[{PayType,PayMethod,PayedMoney}]}]
- 验证码：条件性图片验证码（/GetLoginValidCode 的 Data.ValidCode 非空时需要）；
  登录错误码 8023/8024 表示需要/刷新验证码

注意：
- 一个子域名 = 一个商场（86450002 为商场编码）；后续多店时按账号扩展
- 接口为 HTTP 明文，不走 HTTPS
"""

import base64
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import requests

from utils.http import create_retry_session
from utils.mapping import build_unique_mapping
from utils.mysql_pool import fetch_all_cached


BASE_URL = "http://86450002.v004.youcaihua.net:88"
TIMEOUT = 30

# 需要验证码的登录错误码（前端 login.js：8023/8024 触发刷新验证码）
CAPTCHA_ERROR_CODES = {"8023", "8024"}

# 正式指标：店内收银三种支付方式（rows.PayDetails）+ 盈客宝渠道（CashChannelTypes）
PAY_METHOD_TO_METRIC = {
    "现金支付": "油菜花现金",
    "微信支付-汇付": "油菜花微信",
    "支付宝-汇付": "油菜花支付宝",
}
CHANNEL_TO_METRIC = {
    "盈客宝": "油菜花盈客宝",
}
METRICS = ("油菜花现金", "油菜花微信", "油菜花支付宝", "油菜花盈客宝")


def _as_decimal(value):
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _plain_amount(value):
    amount = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amount == amount.to_integral_value():
        return int(amount)
    return float(amount)


def _normalize_date(value, field_name):
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").strftime(
            "%Y-%m-%d"
        )
    except ValueError as exc:
        raise ValueError(f"{field_name} 必须为 YYYY-MM-DD 格式") from exc


def create_session():
    session = create_retry_session()
    session.trust_env = False
    session.headers.update(
        {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/json",
            "Referer": BASE_URL + "/admin/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "X-Requested-With": "XMLHttpRequest",
        }
    )
    return session


def _response_status(payload):
    """兼容多种响应包装：取 ResponseStatus（可能缺省）。"""
    status = payload.get("ResponseStatus") if isinstance(payload, dict) else None
    if not isinstance(status, dict):
        status = {}
    return status


def _request_json(session, method, path, **kwargs):
    try:
        response = session.request(
            method=method,
            url=BASE_URL + path,
            timeout=TIMEOUT,
            **kwargs,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"油菜花接口请求失败：{path}，{exc}") from None

    if not response.content:
        raise RuntimeError(f"油菜花接口返回空响应：{path}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"油菜花接口未返回 JSON：{path}") from exc

    return payload


def _ensure_success(payload, path):
    """校验业务成功；失败时抛出带 ErrorCode/ErrorMsg 的异常。"""
    status = _response_status(payload)
    code = str(status.get("ErrorCode", ""))
    message = status.get("ErrorMsg") or payload.get("message") or "未知错误"

    ok = payload.get("success")
    if ok is None:
        ok = code in ("0", "", "None") if status else True

    if ok in (True, "true", 1, "1") or code == "0":
        return payload

    if code in CAPTCHA_ERROR_CODES:
        raise RuntimeError(
            f"油菜花登录需要图片验证码（错误码 {code}），当前账号开启了验证码校验，"
            "请到网页端人工登录确认或联系平台关闭"
        )
    raise RuntimeError(f"油菜花接口请求失败：{path}，ErrorCode={code}，{message}")


def login(session, username, password):
    """登录并建立会话 Cookie；密码按前端规则做 Base64 编码。"""
    username = str(username or "").strip()
    password = str(password or "")
    if not username or not password:
        raise ValueError(
            "请通过 main 的 username、password 参数，或 YOUCAIHUA_USERNAME、"
            "YOUCAIHUA_PASSWORD 环境变量提供油菜花账号密码"
        )

    encoded = base64.b64encode(password.encode("utf-8")).decode("ascii")
    payload = _request_json(
        session,
        "POST",
        "/DTOWebLogin",
        params={"username": username, "password": encoded, "ValidCode": ""},
    )
    _ensure_success(payload, "/DTOWebLogin")


def get_mall_info(session):
    """登录后取系统信息：返回 {"mall_name":..., "mall_code":...}，用于场地映射。"""
    try:
        payload = _request_json(
            session, "POST", "/DTOGetSysInfo", params={}
        )
        data = payload.get("Data") if isinstance(payload.get("Data"), dict) else payload
        return {
            "mall_name": str(data.get("MallName") or "").strip(),
            "mall_code": str(data.get("MallCode") or "").strip(),
        }
    except RuntimeError:
        # 商场名拿不到不阻断采集，映射失败时会有明确告警
        return {"mall_name": "", "mall_code": ""}


def get_cash_sum(session, start_date, end_date):
    """收银一览（按操作员×支付方式汇总），返回包含 rows/footer/CashChannelTypes 的字典。

    原始响应为顶层对象（无 Data 包装，包装是前端拦截器做的）。
    """
    body = {
        "StartPeriod": start_date,
        "EndPeriod": end_date,
        "DateType": 99,
    }
    payload = _request_json(
        session,
        "POST",
        "/Finance/DTOGetCashSumByEmp",
        json=body,
    )
    data = payload if isinstance(payload.get("rows"), list) else payload.get("Data")
    if not isinstance(data, dict):
        raise RuntimeError("油菜花收银汇总响应格式异常（找不到 rows）")
    return data


def get_match_data():
    return list(fetch_all_cached(
        "SELECT venue, youcaihua "
        "FROM company_organizational_structure "
        "WHERE youcaihua IS NOT NULL"
    ))


def match_mall_to_venue(mall_name, match_data):
    """youcaihua 映射列存储商场名，换行分隔支持一名多写。"""
    mapping = build_unique_mapping(match_data, "youcaihua")
    if not mall_name:
        return None
    return mapping.get(mall_name)


def aggregate_cash_sum(data, mall_name):
    """
    按正式指标聚合（口径 2026-09-24 与用户确认）：
    - 油菜花现金 / 油菜花微信 / 油菜花支付宝：店内收银渠道 rows.PayDetails
      （剔除"小计/合计"行防重复计数）
    - 油菜花盈客宝：全渠道 CashChannelTypes 中 PayName=盈客宝
    抖音团购/美团大众/快手团购渠道与其他平台重复，不采集。
    返回 (venue_row, emp_detail, channel_types, combos, unknown_methods)
    """
    skip_methods = {"小计", "合计"}
    rows = data.get("rows") or []
    emp_detail = []
    combos = {}
    unknown_methods = {}

    venue_row = {"场地": mall_name or "未匹配"}
    for metric in METRICS:
        venue_row[metric] = Decimal("0")

    for row in rows:
        if not isinstance(row, dict):
            continue
        emp_name = str(row.get("EmpName") or "").strip()
        details = row.get("PayDetails") or []
        emp_amounts = {}
        emp_total = Decimal("0")
        for detail in details:
            if not isinstance(detail, dict):
                continue
            pay_type = str(detail.get("PayType") or "").strip()
            pay_method = str(detail.get("PayMethod") or "").strip()
            if pay_method in skip_methods or pay_type == "合计":
                continue
            amount = _as_decimal(detail.get("PayedMoney"))
            metric = PAY_METHOD_TO_METRIC.get(pay_method)
            if metric:
                venue_row[metric] += amount
            elif pay_method:
                # 未登记的支付方式：不进指标，但打印告警避免静默丢数据
                unknown_methods[pay_method] = unknown_methods.get(pay_method, Decimal("0")) + amount
            if pay_method:
                key = f"{pay_type}/{pay_method}" if pay_type else pay_method
                combos[key] = combos.get(key, Decimal("0")) + amount
                emp_amounts[key] = emp_amounts.get(key, Decimal("0")) + amount
            emp_total += amount
        if emp_name and emp_total:
            emp_detail.append((emp_name, emp_total, emp_amounts))

    for channel in data.get("CashChannelTypes") or []:
        if not isinstance(channel, dict):
            continue
        pay_name = str(channel.get("PayName") or "").strip()
        metric = CHANNEL_TO_METRIC.get(pay_name)
        if metric:
            venue_row[metric] += _as_decimal(channel.get("SysMoney"))

    for metric in METRICS:
        venue_row[metric] = _plain_amount(venue_row[metric])

    channel_types = [
        (str(c.get("PayName") or "").strip(), _as_decimal(c.get("SysMoney")))
        for c in (data.get("CashChannelTypes") or [])
        if isinstance(c, dict)
    ]
    return venue_row, emp_detail, channel_types, combos, unknown_methods


def main(start_date, end_date, username=None, password=None):
    start_date = _normalize_date(start_date, "start_date")
    end_date = _normalize_date(end_date, "end_date")
    if start_date > end_date:
        raise ValueError("start_date 不能晚于 end_date")

    username = username or os.getenv("YOUCAIHUA_USERNAME")
    password = password or os.getenv("YOUCAIHUA_PASSWORD")
    session = create_session()

    try:
        login(session, username, password)
        mall_info = get_mall_info(session)
        data = get_cash_sum(session, start_date, end_date)
    finally:
        session.close()

    mall_name = mall_info.get("mall_name") or ""
    match_data = get_match_data()
    venue = match_mall_to_venue(mall_name, match_data)

    venue_row, emp_detail, channel_types, combos, unknown_methods = aggregate_cash_sum(data, mall_name)
    if venue:
        venue_row["场地"] = venue

    print(f"\n油菜花商场：{mall_name}（{mall_info.get('mall_code')}）→ 场地：{venue or '未匹配'}")
    print(f"账期：{start_date} ~ {end_date}")
    print("\n店内收银支付方式（PayType/PayMethod → 合计）：")
    if combos:
        for key, amount in sorted(combos.items()):
            print(f"- {key}: {_plain_amount(amount)}")
    else:
        print("- 无数据")
    if unknown_methods:
        print("\n警告：发现未登记的支付方式（未计入指标，请确认是否需要新增）：")
        for key, amount in sorted(unknown_methods.items()):
            print(f"- {key}: {_plain_amount(amount)}")
    print("\n全渠道收款（CashChannelTypes，抖音/美团渠道与其他平台重复，不采集）：")
    if channel_types:
        for name, amount in channel_types:
            print(f"- {name}: {_plain_amount(amount)}")
    else:
        print("- 无数据")
    print("\n操作员非零明细：")
    if emp_detail:
        for emp_name, emp_total, _amounts in emp_detail:
            print(f"- {emp_name}: {_plain_amount(emp_total)}")
    else:
        print("- 无数据")
    if not venue:
        print("\n警告：MySQL company_organizational_structure.youcaihua 列未配置该商场名，无法映射场地")

    print("\n返回：", venue_row)
    return [venue_row]


if __name__ == "__main__":
    # 单独运行测试：python crawlers/youcaihua_crawler.py 2026-09-01 2026-09-24
    # 账号密码从环境变量读取（YOUCAIHUA_USERNAME / YOUCAIHUA_PASSWORD）
    import sys
    if len(sys.argv) >= 3:
        start, end = sys.argv[1], sys.argv[2]
    else:
        d = datetime.now().strftime("%Y-%m-%d")
        start, end = d, d
    data = main(start, end)
    print(f"\n共返回 {len(data)} 条场地数据")
