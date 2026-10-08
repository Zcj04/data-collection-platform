# -*- coding: utf-8 -*-
"""
鲸舰（jingjianx.vip）数据采集（原八爪鱼脚本整理）

主入口：main(start_date, end_date, username, password) -> List[Dict]
    输入：起止日期 YYYY-MM-DD + 鲸舰账号密码（参数传入）
    输出：[{"场地": str, "总实收":..., "鲸舰现金":..., "鲸舰微信":..., "鲸舰支付宝":..., "鲸舰Payme":..., "鲸舰非现金":..., "鲸舰出币":..., "鲸舰投币":..., "鲸舰出货":..., "鲸舰积分增加":..., "鲸舰积分减少":...}, ...]（场地维度）

流程：登录→获取店铺列表→逐一切换店铺(新Token)→拉营收(日期提前1天)+设备数据+积分→MySQL whale_ship列场地映射→返回

注意：
- 两个BASE_URL（登录与数据分离域名）
- 日期特殊处理：end_date+1天，营收接口日期整体提前1天
- 账号密码参数传入（规范），适配器从凭证管理注入
- 核心业务逻辑保持原样，未做功能性改动
"""

import logging
import os
from urllib.parse import urlsplit

import requests
from core.config import get_platform_config
from utils.http import create_retry_session
from utils.mapping import add_unique_mapping
from utils.mysql_pool import fetch_all_cached
from datetime import datetime, timedelta


_PLATFORM_CONFIG = get_platform_config("jingjian")
LOGIN_BASE_URL = (
    os.environ.get("JINGJIAN_LOGIN_BASE_URL")
    or _PLATFORM_CONFIG.get("login_base_url")
    or "http://21359-grabmono.jingjianx.vip"
).rstrip("/")
DATA_BASE_URL = (
    os.environ.get("JINGJIAN_DATA_BASE_URL")
    or _PLATFORM_CONFIG.get("data_base_url")
    or _PLATFORM_CONFIG.get("base_url")
    or "http://grabmono.jingjianx.vip"
).rstrip("/")
TIMEOUT = 30
logger = logging.getLogger(__name__)


def _insecure_http_allowed():
    return os.environ.get(
        "WORKBUDDY_ALLOW_INSECURE_JINGJIAN_HTTP", ""
    ).strip().lower() in {"1", "true", "yes", "on"}


def _validate_transport_policy():
    """拒绝无意间通过公网 HTTP 发送鲸舰密码和 Bearer Token。"""
    urls = (LOGIN_BASE_URL, DATA_BASE_URL)
    invalid = [url for url in urls if urlsplit(url).scheme.lower() not in {"http", "https"}]
    if invalid:
        raise RuntimeError("鲸舰服务地址必须使用 http:// 或 https://")

    insecure = [url for url in urls if urlsplit(url).scheme.lower() == "http"]
    if not insecure:
        return
    if not _insecure_http_allowed():
        raise RuntimeError(
            "鲸舰平台仅提供 HTTP，默认已阻止发送账号、密码和 Token；"
            "请优先通过 JINGJIAN_LOGIN_BASE_URL/JINGJIAN_DATA_BASE_URL "
            "配置公司 HTTPS 网关。确认链路已通过专线、VPN 或受控内网隔离后，"
            "才可显式设置 WORKBUDDY_ALLOW_INSECURE_JINGJIAN_HTTP=1"
        )
    logger.warning("鲸舰正在使用显式允许的 HTTP 兼容模式；请确保链路已隔离")


def shift_date(date_value, days):
    date_text = str(date_value).strip()

    for date_format in (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            date_object = datetime.strptime(
                date_text,
                date_format,
            )
            return (
                date_object + timedelta(days=days)
            ).strftime(date_format)
        except ValueError:
            continue

    raise ValueError(
        "日期格式必须为 YYYY-MM-DD "
        "或 YYYY-MM-DD HH:MM:SS"
    )


def add_one_day(date_value):
    return shift_date(date_value, 1)


def subtract_one_day(date_value):
    return shift_date(date_value, -1)


def request_json(
    session,
    method,
    url,
    headers=None,
    params=None,
    json_data=None,
):
    response = session.request(
        method=method,
        url=url,
        headers=headers,
        params=params,
        json=json_data,
        timeout=TIMEOUT,
    )
    response.raise_for_status()

    result = response.json()

    if (
        isinstance(result, dict)
        and result.get("success") is False
    ):
        raise RuntimeError(
            result.get("msg") or "接口请求失败"
        )

    return result


def get_headers(token=None):
    headers = {
        "User-Agent": "Apifox/1.0.0 (https://apifox.com)",
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Connection": "keep-alive",
    }

    if token:
        headers["Authorization"] = f"Bearer {token}"

    return headers


def login(session, username, password):
    url = (
        LOGIN_BASE_URL
        + "/basic/manager/login/account"
    )

    response_data = request_json(
        session=session,
        method="POST",
        url=url,
        headers=get_headers(),
        json_data={
            "userName": str(username).strip(),
            "password": str(password),
        },
    )

    token = (
        response_data.get("data") or {}
    ).get("token")

    if not token:
        raise RuntimeError("登录后没有获取到 Token")

    return token


def get_all_shops(session, token):
    url = LOGIN_BASE_URL + "/basic/shop/getshops"

    response_data = request_json(
        session=session,
        method="GET",
        url=url,
        headers=get_headers(token),
        params={
            "limit": 99999,
            "keywords": "",
        },
    )

    shops = response_data.get("data") or []

    if not isinstance(shops, list):
        raise RuntimeError("店铺列表格式错误")

    return shops


def switch_shop(session, token, shop_id):
    url = (
        LOGIN_BASE_URL
        + "/basic/manager/login/switch/shop"
    )

    response_data = request_json(
        session=session,
        method="POST",
        url=url,
        headers=get_headers(token),
        json_data={
            "shopId": shop_id,
        },
    )

    shop_data = response_data.get("data") or {}

    if not shop_data.get("token"):
        raise RuntimeError("切换店铺后没有获取到 Token")

    return shop_data


def get_payment_prop_map(headers):
    """从 headers 中递归解析「支付方式 label -> prop key」的映射。

    微信 / 支付宝 / Payme 的 prop 形如
    ``saobeiInternationalMultiExecut_<32位数字>_<32位数字>``，
    尾部数字每次查询都会变化，因此不能写死 key，
    必须根据 label 动态获取对应的 prop。
    """
    payment_labels = {"Payme", "支付宝", "微信"}
    prop_map = {}

    def walk(items):
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            label = item.get("label")
            prop = item.get("prop")
            if label in payment_labels and prop:
                prop_map[label] = prop
            # 父级分组（如「系统销售」）的支付方式在 childrens 里
            walk(item.get("childrens"))

    walk(headers)
    return prop_map


def get_one_shop_data(
    session,
    start_date,
    end_date,
    revenue_start_date,
    revenue_end_date,
    token,
    shop_name,
):
    # 1. 总实收、现金、非现金、出币
    # 注意：营收接口的日期和其它接口不一致，要整体提前一天。
    revenue_url = (
        DATA_BASE_URL
        + "/finance/manager/revenueoverview/revenue"
    )

    revenue_result = request_json(
        session=session,
        method="GET",
        url=revenue_url,
        headers=get_headers(token),
        params={
            "startDate": revenue_start_date,
            "endDate": revenue_end_date,
        },
    )

    revenue_data_obj = (
        revenue_result.get("data") or {}
    )
    revenue_list = revenue_data_obj.get(
        "dataXs", []
    )

    if not revenue_list:
        raise RuntimeError("营收接口没有返回数据")

    revenue_data = revenue_list[-1]

    real_money = round(
        float(revenue_data.get("realMoney") or 0),
        2,
    )
    cash_real_money = round(
        float(
            revenue_data.get("cashRealMoney") or 0
        ),
        2,
    )

    # 微信 / 支付宝 / Payme 的 prop 不固定，需从 headers 动态解析
    prop_map = get_payment_prop_map(
        revenue_data_obj.get("headers", [])
    )

    def get_payment_amount(label):
        prop = prop_map.get(label)
        if not prop:
            return 0.0
        try:
            return float(
                revenue_data.get(prop) or 0
            )
        except (ValueError, TypeError):
            return 0.0

    wechat_money = round(
        get_payment_amount("微信"),
        2,
    )
    alipay_money = round(
        get_payment_amount("支付宝"),
        2,
    )
    payme_money = round(
        get_payment_amount("Payme"),
        2,
    )

    # 非现金 = 微信 + 支付宝 + Payme
    no_cash_real_money = round(
        wechat_money + alipay_money + payme_money,
        2,
    )

    sell_coin_amount = int(
        float(
            revenue_data.get("sellCoinAmount") or 0
        )
    )

    # 2. 投币、出货
    machine_url = (
        DATA_BASE_URL
        + "/device/manager/machineplaylog/getsummarylist"
    )

    machine_result = request_json(
        session=session,
        method="POST",
        url=machine_url,
        headers=get_headers(token),
        json_data={
            "startDate": start_date,
            "endDate": end_date,
            "KindIdStr": "",
            "isCalculate": True,
        },
    )

    machine_foot_data = (
        machine_result.get("footData") or {}
    )

    winning_coins = int(
        float(
            machine_foot_data.get("winningCoins")
            or 0
        )
    )
    out_gift_total_amount = int(
        float(
            machine_foot_data.get(
                "outGiftTotalAmount"
            )
            or 0
        )
    )

    # 3. 积分增加、积分减少
    points_url = (
        DATA_BASE_URL
        + "/member/manager/memberstore/getstorechangelog"
    )

    points_change_list = []

    for flow_type in (1, 2):
        points_result = request_json(
            session=session,
            method="GET",
            url=points_url,
            headers=get_headers(token),
            params={
                "startTime": start_date,
                "endTime": end_date,
                "businessTypeContent": "",
                "flowType": flow_type,
                "category": 105,
                "page": 1,
                "limit": 20,
            },
        )

        points_foot_data = (
            points_result.get("footData") or {}
        )
        amount = (
            points_foot_data.get("amount") or 0
        )

        points_change_list.append(
            int(abs(float(amount)))
        )

    return {
        "鲸舰店铺名": shop_name,
        "总实收": real_money,
        "鲸舰现金": cash_real_money,
        "鲸舰微信": wechat_money,
        "鲸舰支付宝": alipay_money,
        "鲸舰Payme": payme_money,
        "鲸舰非现金": no_cash_real_money,
        "鲸舰出币": sell_coin_amount,
        "鲸舰投币": winning_coins,
        "鲸舰出货": out_gift_total_amount,
        "鲸舰积分增加": points_change_list[0],
        "鲸舰积分减少": points_change_list[1],
    }


def get_venue_mapping():
    sql = """
        SELECT venue, whale_ship
        FROM company_organizational_structure
        WHERE whale_ship IS NOT NULL
    """
    mapping = {}
    ambiguous = set()
    for venue, whale_ship in fetch_all_cached(sql):
        add_unique_mapping(mapping, ambiguous, whale_ship, venue, "jingjian")
    return mapping


def main(start_date, end_date, username, password):
    _validate_transport_policy()
    # 其它接口仍沿用原逻辑：
    # 传入 2026-07-01 ~ 2026-07-07，请求 2026-07-01 ~ 2026-07-08。
    start_date = str(start_date).strip()
    end_date = add_one_day(end_date)

    # 营收接口日期单独提前一天：
    # 其它接口 2026-07-01 ~ 2026-07-08；
    # 营收接口 2026-06-30 ~ 2026-07-07。
    revenue_start_date = start_date
    revenue_end_date = subtract_one_day(end_date)

    print(
        "鲸舰请求日期：",
        {
            "其它接口": {
                "startDate": start_date,
                "endDate": end_date,
            },
            "营收接口": {
                "startDate": revenue_start_date,
                "endDate": revenue_end_date,
            },
        },
    )

    session = create_retry_session()

    try:
        current_token = login(
            session,
            username,
            password,
        )

        shops = get_all_shops(
            session,
            current_token,
        )

        data_list = []

        for shop in shops:
            shop_id = shop.get("shopId")
            shop_name = shop.get("shopName") or ""

            switched_data = switch_shop(
                session,
                current_token,
                shop_id,
            )

            current_token = switched_data["token"]
            shop_name = (
                switched_data.get("shopName")
                or shop_name
            )

            shop_data = get_one_shop_data(
                session=session,
                start_date=start_date,
                end_date=end_date,
                revenue_start_date=revenue_start_date,
                revenue_end_date=revenue_end_date,
                token=current_token,
                shop_name=shop_name,
            )

            data_list.append(shop_data)

        venue_mapping = get_venue_mapping()
        result = []

        for shop_data in data_list:
            item = shop_data.copy()

            shop_name = str(
                item.pop("鲸舰店铺名", "") or ""
            ).strip()

            item["场地"] = venue_mapping.get(
                shop_name,
                shop_name,
            )

            result.append(item)

        return result

    finally:
        session.close()


if __name__ == "__main__":
    import sys
    from datetime import datetime
    username = os.environ.get("JINGJIAN_USERNAME", "")
    password = os.environ.get("JINGJIAN_PASSWORD", "")
    if not username or not password:
        print("请设置环境变量 JINGJIAN_USERNAME 和 JINGJIAN_PASSWORD")
        sys.exit(1)
    if len(sys.argv) >= 3:
        start, end = sys.argv[1], sys.argv[2]
    else:
        d = datetime.now().strftime("%Y-%m-%d")
        start, end = d, d
    data = main(start, end, username, password)
    print(f"\n共返回 {len(data)} 条场地数据")
