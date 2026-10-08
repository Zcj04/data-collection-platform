# -*- coding: utf-8 -*-
"""
乐摇摇（leyaoyao）数据采集（原八爪鱼脚本整理）

主入口：main(start_date, end_date, username, password) -> List[Dict]
    输入：起止日期 YYYY-MM-DD + 乐摇摇账号密码（参数传入或环境变量）
    输出：[{"场地": str, "乐摇摇非现金":..., "乐摇摇现金":..., "乐摇摇手续费":..., "乐摇摇投币":..., "乐摇摇出货":...}, ...]（场地维度，5个指标）

流程：登录(MD5密码)→获取设备组列表→按组拉订单数据(日报/自定义)→MySQL场地映射→聚合返回

注意：
- 账号密码支持参数传入 + 环境变量 LEYAOYAO_USERNAME/LEYAOYAO_PASSWORD
- 单文件自包含，代码质量高，无需拆分为多文件
- 核心业务逻辑保持原样，未做功能性改动
"""

import hashlib
import os
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from utils.mapping import add_unique_mapping
from utils.http import create_retry_session
from utils.mysql_pool import fetch_all_cached
from utils.redaction import redact_sensitive_text
import requests


BASE_URL = "https://b.leyaoyao.com"
TIMEOUT = 30


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


def _as_int(value):
    return int(_as_decimal(value))


def _normalize_date(value, field_name):
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").strftime(
            "%Y-%m-%d"
        )
    except ValueError as exc:
        raise ValueError(f"{field_name} 必须为 YYYY-MM-DD 格式") from exc


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
        # URL 含上游要求的登录参数；保留异常类型/response 供重试判断。
        exc.args = (redact_sensitive_text(str(exc)),)
        raise exc from None

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"乐摇摇接口未返回 JSON：{path}") from exc

    code = payload.get("code")
    result = payload.get("result")
    success_codes = (None, 0, "0", 200, "200")
    if code not in success_codes or result not in success_codes:
        message = (
            payload.get("message")
            or payload.get("description")
            or "未知错误"
        )
        raise RuntimeError(
            f"乐摇摇接口请求失败：{path}，code={code}，result={result}，{message}"
        )

    return payload


def create_session():
    session = create_retry_session()
    session.trust_env = False
    session.headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": BASE_URL + "/merchant-saas/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "authorization-system": "3",
        }
    )
    return session


def login(session, username, password):
    username = str(username or "").strip()
    password = str(password or "")
    if not username or not password:
        raise ValueError(
            "请通过 main 的 username、password 参数，或 LEYAOYAO_USERNAME、"
            "LEYAOYAO_PASSWORD 环境变量提供乐摇摇账号密码"
        )

    response_data = _request_json(
        session,
        "POST",
        "/lyy/rest/group/distributor/login",
        params={
            "userName": username,
            "password": hashlib.md5(password.encode("utf-8")).hexdigest(),
            "verifyCode": "",
        },
        headers={"authorization-identifier": uuid.uuid4().hex[:9]},
        json={},
    )

    login_data = response_data.get("data") or {}
    authorization_bar = login_data.get("ticket")
    if not authorization_bar:
        raise RuntimeError("乐摇摇登录成功，但响应中未返回 authorization-bar")

    session.headers.update({"authorization-bar": str(authorization_bar)})


def get_equipment_groups(session):
    response_data = _request_json(
        session,
        "GET",
        "/lyy/rest/benefitAnalysis/getEquipmentGroups",
        params={"all": "true"},
    )
    groups = response_data.get("data") or []
    if isinstance(groups, dict):
        groups = groups.get("list") or groups.get("groups") or []

    result = []
    seen_group_ids = set()
    for group in groups:
        if not isinstance(group, dict):
            continue

        group_id = group.get("groupId")
        if group_id in (None, "") or group_id in seen_group_ids:
            continue

        seen_group_ids.add(group_id)
        result.append(
            {
                "group_id": str(group_id),
                "group_name": str(group.get("groupName") or "").strip(),
            }
        )

    if not result:
        raise RuntimeError("未获取到可查询的乐摇摇场地")

    return result


def get_group_order_data(session, group_id, start_date, end_date):
    if start_date == end_date:
        # 日报模式：只传 day，latitude=1
        params = {
            "groupIds": group_id,
            "timeSta": start_date,
            "day": start_date,
            "latitude": 1,
            "showCash": "true",
            "showSubsidy": "true",
        }
    else:
        # 自定义时间模式：传 timeSta 和 timeEnd，latitude=6
        params = {
            "groupIds": group_id,
            "timeSta": start_date,
            "latitude": 6,
            "timeEnd": end_date,
            "showCash": "true",
            "showSubsidy": "false",
        }

    response_data = _request_json(
        session,
        "GET",
        "/gw/oneData/merchant/mobile/v2/order",
        params=params,
    )
    data = response_data.get("data") or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"场地 {group_id} 的订单统计响应格式异常")

    return {
        "online_amount": _as_decimal(data.get("onlinePayAmount")),
        "cash_amount": _as_decimal(data.get("cashPayAmount")),
        "coin_count": _as_int(data.get("coinsSumNumber")),
        "gift_count": _as_int(data.get("giftConsumptionNumber")),
    }


def get_all_group_data(session, start_date, end_date):
    group_data = []

    for group in get_equipment_groups(session):
        amounts = get_group_order_data(
            session,
            group["group_id"],
            start_date,
            end_date,
        )
        group_data.append({**group, **amounts})

    return group_data


def get_match_data():
    return list(fetch_all_cached(
        "SELECT venue, leyaoyao "
        "FROM company_organizational_structure "
        "WHERE leyaoyao IS NOT NULL"
    ))


def _build_group_to_venue(match_data):
    group_to_venue = {}
    ambiguous = set()
    for venue, group_names in match_data:
        for group_name in str(group_names or "").splitlines():
            group_name = group_name.strip()
            if group_name:
                add_unique_mapping(group_to_venue, ambiguous, group_name, venue, "leyaoyao")
    return group_to_venue


def match_leyaoyao_groups(group_data, match_data):
    group_to_venue = _build_group_to_venue(match_data)
    venue_totals = {}
    unmatched_groups = []

    for group in group_data:
        group_name = group["group_name"]
        venue = group_to_venue.get(group_name)
        if venue is None:
            if any(
                group[key] != 0
                for key in (
                    "online_amount",
                    "cash_amount",
                    "coin_count",
                    "gift_count",
                )
            ):
                unmatched_groups.append(
                    f"{group_name or '未命名场地'} ({group['group_id']})"
                )
            continue

        totals = venue_totals.setdefault(
            venue,
            {
                "online_amount": Decimal("0"),
                "cash_amount": Decimal("0"),
                "coin_count": 0,
                "gift_count": 0,
            },
        )
        totals["online_amount"] += group["online_amount"]
        totals["cash_amount"] += group["cash_amount"]
        totals["coin_count"] += group["coin_count"]
        totals["gift_count"] += group["gift_count"]

    result = []
    for venue, totals in venue_totals.items():
        online_amount = totals["online_amount"]
        result.append(
            {
                "场地": venue,
                "乐摇摇非现金": _plain_amount(online_amount),
                "乐摇摇现金": _plain_amount(totals["cash_amount"]),
                "乐摇摇手续费": _plain_amount(online_amount * Decimal("0.006")),
                "乐摇摇投币": totals["coin_count"],
                "乐摇摇出货": totals["gift_count"],
            }
        )

    return result, unmatched_groups


def main(start_date, end_date, username=None, password=None):
    start_date = _normalize_date(start_date, "start_date")
    end_date = _normalize_date(end_date, "end_date")
    if start_date > end_date:
        raise ValueError("start_date 不能晚于 end_date")

    username = username or os.getenv("LEYAOYAO_USERNAME")
    password = password or os.getenv("LEYAOYAO_PASSWORD")
    session = create_session()

    try:
        login(session, username, password)
        group_data = get_all_group_data(session, start_date, end_date)
    finally:
        session.close()

    result, unmatched_groups = match_leyaoyao_groups(
        group_data,
        get_match_data(),
    )

    print("\n未匹配到的乐摇摇场地（有金额且不全为 0）：")
    if unmatched_groups:
        for group_name in unmatched_groups:
            print(f"- {group_name}")
    else:
        print("无未匹配场地（或所有未匹配场地金额均为 0）")

    print(result)
    return result


if __name__ == "__main__":
    # 单独运行测试：python crawlers/leyaoyao_crawler.py 2026-08-01 2026-08-06
    # 账号密码从环境变量读取（LEYAOYAO_USERNAME / LEYAOYAO_PASSWORD）
    import sys
    if len(sys.argv) >= 3:
        start, end = sys.argv[1], sys.argv[2]
    else:
        d = datetime.now().strftime("%Y-%m-%d")
        start, end = d, d
    data = main(start, end)
    print(f"\n共返回 {len(data)} 条场地数据")
