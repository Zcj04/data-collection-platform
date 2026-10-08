# -*- coding: utf-8 -*-
"""
StarThing（starthing.com）数据采集（原八爪鱼脚本整理）

主入口：main(start_date, end_date, account, password) -> List[Dict]
    输入：起止日期 YYYY-MM-DD + StarThing 账号密码（参数传入）
    输出：[{"场地": str, "StarThing总收款":..., "StarThing非现金":..., ...}]（场地维度，9指标）

流程：登录(MD5双重加密)→获取门店列表→逐店拉营收/支付/币/积分/机台数据→弹珠机拆分(B店店)→MySQL star_thing列场地映射→返回

特殊逻辑：
- 弹珠机：从"香港C店"/"香港C站"拆分出"香港B店"独立记录
- 机台数据分页获取（size=100）
- 核心业务逻辑保持原样，未做功能性改动
"""

import hashlib
from decimal import Decimal, InvalidOperation

import os
import requests
from utils.http import create_retry_session
from utils.mapping import add_unique_mapping
from utils.mysql_pool import fetch_all_cached


BASE_URL = "https://pro.starthing.com"
RAM_SYSTEM = os.getenv("STARTHING_RAM_SYSTEM", "")
VERIFY_CODE = os.getenv("STARTHING_VERIFY_CODE", "")
TIMEOUT = 30
FEE_RATE = Decimal("0.014")  # StarThing手续费率（非现金 × 0.014）
PINBALL_EQUIPMENT_ID = os.getenv("STARTHING_PINBALL_EQUIPMENT_ID", "")
PINBALL_TENANT_ID = os.getenv("STARTHING_PINBALL_TENANT_ID", "")
PINBALL_ORG_ID = os.getenv("STARTHING_PINBALL_ORG_ID", "")
PINBALL_SOURCE_STORE_NAMES = {
    "香港C店",
    "香港C站",
}
PINBALL_STORE_NAME = "香港B店"


RESULT_KEYS = [
    "StarThing总收款",
    "StarThing非现金",
    "StarThing手续费",
    "StarThing现金",
    "StarThing出币",
    "StarThing收币",
    "StarThing积分增加",
    "StarThing积分减少",
    "StarThing机台投币",
    "StarThing出货",
]


def _md5(value):
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def _encrypt_password(password):
    return _md5(_md5(str(password)) + VERIFY_CODE)


def _request_json(session, method, path, headers=None, **kwargs):
    response = session.request(
        method,
        BASE_URL + path,
        headers=headers,
        timeout=TIMEOUT,
        **kwargs,
    )
    response.raise_for_status()

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"StarThing接口返回的不是JSON：{path}"
        ) from exc

    code = data.get("code", data.get("result"))
    if code not in (None, 0, 200, 203, 204, "0000000"):
        message = (
            data.get("message")
            or data.get("errorMessage")
            or "未知错误"
        )
        raise RuntimeError(
            f"StarThing接口请求失败：{path}，code={code}，message={message}"
        )

    return data


def _login(session, account, password):
    payload = {
        "account": str(account).strip(),
        "password": _encrypt_password(password),
        "verifyCode": VERIFY_CODE,
        "clientType": "pc",
        "authSystemId": RAM_SYSTEM,
    }
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "ram-system": RAM_SYSTEM,
        "ram-token": "",
        "ram-tenant": "",
        "x-accept-language": "zh-CN",
    }

    data = _request_json(
        session,
        "POST",
        "/gw/ram-service/sso/login",
        headers=headers,
        json=payload,
    )
    body = data.get("body") or {}
    token = body.get("token")

    if not token:
        raise RuntimeError("StarThing登录成功响应中没有token")

    return str(token)


def _auth_headers(token, tenant_id=None, org_id=None):
    headers = {
        "Accept": "application/json, text/plain, */*",
        "ram-system": RAM_SYSTEM,
        "ram-token": str(token),
        "x-accept-language": "zh-CN",
    }
    if tenant_id is not None:
        headers["ram-tenant"] = str(tenant_id)
    if org_id is not None:
        headers["ram-org"] = str(org_id)
    return headers


def _walk_orgs(orgs):
    for org in orgs or []:
        if not isinstance(org, dict):
            continue

        yield org
        for key in ("children", "orgList", "tenantOrgList"):
            children = org.get(key)
            if isinstance(children, list):
                yield from _walk_orgs(children)


def _get_all_stores(session, token):
    data = _request_json(
        session,
        "GET",
        "/gw/ram-service/permission/account/tenants",
        headers=_auth_headers(token),
    )
    body = data.get("body") or []
    if isinstance(body, dict):
        tenants = (
            body.get("records")
            or body.get("list")
            or body.get("tenantList")
            or []
        )
    else:
        tenants = body

    stores = []
    seen = set()

    for tenant in tenants:
        if not isinstance(tenant, dict):
            continue
        if str(tenant.get("state")) == "2":
            continue

        tenant_id = tenant.get("tenantId")
        if tenant_id is None:
            continue

        for org in _walk_orgs(tenant.get("tenantOrgList")):
            if str(org.get("type")) != "4":
                continue
            if str(org.get("state")) == "2":
                continue

            org_id = org.get("authOrgId")
            if org_id is None:
                continue

            unique_key = (str(tenant_id), str(org_id))
            if unique_key in seen:
                continue
            seen.add(unique_key)

            stores.append(
                {
                    "tenant_id": str(tenant_id),
                    "org_id": str(org_id),
                    "name": str(org.get("name") or "").strip(),
                }
            )

    if not stores:
        raise RuntimeError("当前StarThing账号下没有可访问的门店")

    return stores


def _as_decimal(value):
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _plain_number(value):
    number = _as_decimal(value)
    if number == number.to_integral_value():
        return int(number)
    return float(number)


def _body_rows(body):
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        rows = body.get("records") or body.get("list")
        if isinstance(rows, list):
            return rows
    return []


def _last_row(body):
    rows = _body_rows(body)
    return rows[-1] if rows else {}


def _get_store_data(session, token, store, start_date, end_date):
    headers = _auth_headers(
        token,
        tenant_id=store["tenant_id"],
        org_id=store["org_id"],
    )
    date_params = {
        "startDate": start_date,
        "endDate": end_date,
        "RefreshDisabled": "false",
    }

    employee_data = _request_json(
        session,
        "GET",
        "/gw/entertainment/pc/revenue/statistics/employee",
        headers=headers,
        params=date_params,
    )
    employee_rows = _body_rows(employee_data.get("body"))
    store_name = store["name"]
    if employee_rows:
        store_name = (
            str(employee_rows[0].get("accountName") or "").strip()
            or store_name
        )

    overview_data = _request_json(
        session,
        "GET",
        "/gw/entertainment/pc/revenue/statistics/overview",
        headers=headers,
        params=date_params,
    )
    overview_body = overview_data.get("body") or {}
    revenue_total = overview_body.get("revenueTotal") or {}
    total_payment = _as_decimal(revenue_total.get("currentValue"))

    common_params = {
        "startDate": start_date,
        "endDate": end_date,
        "RefreshDisabled": "true",
        "hasCoin": "true",
        "hasPinball": "false",
    }

    payment_data = _request_json(
        session,
        "GET",
        "/gw/entertainment/pc/revenue/statistics/payment",
        headers=headers,
        params=common_params,
    )
    cash_payment = Decimal("0")
    for payment in _body_rows(payment_data.get("body")):
        if payment.get("paymentTypeName") == "现金支付":
            cash_payment += _as_decimal(payment.get("actualAmount"))

    coin_data = _request_json(
        session,
        "GET",
        "/gw/entertainment/pc/statistics/benefit/coinBenefit/range",
        headers=headers,
        params=common_params,
    )
    coin_row = _last_row(coin_data.get("body"))
    coin_out = _plain_number(coin_row.get("coinOut"))
    coin_in = _plain_number(coin_row.get("coinIn"))

    point_data = _request_json(
        session,
        "GET",
        "/gw/entertainment/pc/statistics/benefit/pointBenefit/range",
        headers=headers,
        params=common_params,
    )
    point_row = _last_row(point_data.get("body"))
    point_out = _plain_number(point_row.get("pointOut"))
    point_in = _plain_number(point_row.get("pointIn"))

    machine_coin, gift_count = _get_machine_totals(
        session,
        headers,
        start_date,
        end_date,
    )

    return {
        "StarThing店铺名": store_name,
        "StarThing总收款": _plain_number(total_payment),
        "StarThing非现金": _plain_number(total_payment - cash_payment),
        "StarThing手续费": _plain_number(
            (total_payment - cash_payment) * FEE_RATE
        ),
        "StarThing现金": _plain_number(cash_payment),
        "StarThing出币": coin_out,
        "StarThing收币": coin_in,
        "StarThing积分增加": point_out,
        "StarThing积分减少": point_in,
        "StarThing机台投币": machine_coin,
        "StarThing出货": gift_count,
    }


def _get_pinball_payment(
    session,
    token,
    start_date,
    end_date,
):
    headers = _auth_headers(
        token,
        tenant_id=PINBALL_TENANT_ID,
        org_id=PINBALL_ORG_ID,
    )
    data = _request_json(
        session,
        "GET",
        "/gw/entertainment/pc/revenue/statistics/equipment",
        headers=headers,
        params={
            "size": 10,
            "current": 1,
            "equipmentIds": PINBALL_EQUIPMENT_ID,
            "startDate": start_date,
            "endDate": end_date,
        },
    )
    rows = _body_rows(data.get("body"))
    if not rows:
        return None

    actual_amount = _as_decimal(
        rows[0].get("actualAmount")
    )
    cash_amount = _as_decimal(
        rows[0].get("cashActualAmount")
    )
    if actual_amount < 0 or cash_amount < 0:
        raise RuntimeError("B店弹珠机收款不能为负数")
    if cash_amount > actual_amount:
        raise RuntimeError(
            "B店弹珠机现金收款大于总收款"
        )

    return {
        "StarThing总收款": actual_amount,
        "StarThing非现金": actual_amount - cash_amount,
        "StarThing手续费": _plain_number(
            (actual_amount - cash_amount) * FEE_RATE
        ),
        "StarThing现金": cash_amount,
    }


def _split_pinball_payment(data_list, pinball_payment):
    source_item = next(
        (
            item
            for item in data_list
            if str(
                item.get("StarThing店铺名") or ""
            ).strip() in PINBALL_SOURCE_STORE_NAMES
        ),
        None,
    )
    if source_item is None:
        available_names = "、".join(
            str(
                item.get("StarThing店铺名") or ""
            ).strip()
            for item in data_list
        )
        raise RuntimeError(
            "未找到B店弹珠机的源店铺（{}）；当前店铺：{}".format(
                "、".join(sorted(PINBALL_SOURCE_STORE_NAMES)),
                available_names,
            )
        )

    for key in (
        "StarThing总收款",
        "StarThing非现金",
        "StarThing现金",
    ):
        source_amount = _as_decimal(
            source_item.get(key)
        )
        split_amount = _as_decimal(
            pinball_payment.get(key)
        )
        if split_amount > source_amount:
            raise RuntimeError(
                "{}的{}为{}，小于B店弹珠机待拆分金额{}".format(
                    source_item.get("StarThing店铺名"),
                    key,
                    _plain_number(source_amount),
                    _plain_number(split_amount),
                )
            )
        source_item[key] = _plain_number(
            source_amount - split_amount
        )

    pinball_item = {
        "StarThing店铺名": PINBALL_STORE_NAME,
        "_fallback_venue": PINBALL_STORE_NAME,
    }
    for key in RESULT_KEYS:
        pinball_item[key] = _plain_number(
            pinball_payment.get(key, 0)
        )
    data_list.append(pinball_item)


def _get_machine_totals(session, headers, start_date, end_date):
    size = 100
    current = 1
    all_coin_sum = Decimal("0")
    gift_sum = Decimal("0")

    while True:
        payload = {
            "size": size,
            "current": current,
            "sort": [{"field": "allCoinSum", "orderBy": "desc"}],
            "startDate": start_date,
            "endDate": end_date,
            "RefreshDisabled": True,
        }
        data = _request_json(
            session,
            "POST",
            "/gw/entertainment/pc/statistics/gift/ranking/equipmentGroupPage",
            headers={**headers, "Content-Type": "application/json"},
            json=payload,
        )
        body = data.get("body") or {}
        records = _body_rows(body)

        for record in records:
            all_coin_sum += _as_decimal(record.get("allCoinSum"))
            gift_sum += _as_decimal(record.get("giftSum"))

        total = _as_decimal(
            body.get("total") if isinstance(body, dict) else len(records)
        )
        if not records or len(records) < size:
            break
        if total and Decimal(current * size) >= total:
            break

        current += 1

    return _plain_number(all_coin_sum), _plain_number(gift_sum)


def _get_match_tuples():
    return list(fetch_all_cached(
        "SELECT venue, star_thing "
        "FROM company_organizational_structure "
        "WHERE star_thing IS NOT NULL;"
    ))


def _match_star_thing_lists(data_list, match_tuples):
    shop_to_site = {}
    ambiguous = set()
    for site_name, shop_names in match_tuples:
        for shop_name in str(shop_names).splitlines():
            shop_name = shop_name.strip()
            if shop_name:
                add_unique_mapping(shop_to_site, ambiguous, shop_name, site_name, "starthing")

    result = []
    unmatched_shops = []

    for item in data_list:
        shop_name = item.get("StarThing店铺名", "")
        site_name = (
            shop_to_site.get(shop_name)
            or item.get("_fallback_venue")
        )

        if site_name is not None:
            matched_item = {"场地": site_name}
            for key in RESULT_KEYS:
                matched_item[key] = item.get(key, 0)
            result.append(matched_item)
            continue

        if any(_as_decimal(item.get(key)) != 0 for key in RESULT_KEYS):
            unmatched_shops.append(shop_name)

    return result, unmatched_shops


def main(start_date, end_date, account, password):
    start_date = str(start_date).strip()
    end_date = str(end_date).strip()
    if not start_date or not end_date:
        raise ValueError("start_date和end_date不能为空")

    session = create_retry_session()
    session.trust_env = False
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/149.0.0.0 Safari/537.36"
            ),
            "Origin": BASE_URL,
            "Referer": BASE_URL + "/",
        }
    )

    try:
        token = _login(session, account, password)
        stores = _get_all_stores(session, token)
        data_list = [
            _get_store_data(
                session,
                token,
                store,
                start_date,
                end_date,
            )
            for store in stores
        ]
        pinball_payment = _get_pinball_payment(
            session,
            token,
            start_date,
            end_date,
        )
        if pinball_payment is not None:
            _split_pinball_payment(data_list, pinball_payment)
        else:
            print("B店弹珠机无数据，跳过拆分（不添加B店店记录）")
    finally:
        session.close()

    match_tuples = _get_match_tuples()
    result, unmatched_shops = _match_star_thing_lists(
        data_list,
        match_tuples,
    )

    print("\n未匹配到的店铺（有值且不全为0）:")
    if unmatched_shops:
        for shop_name in unmatched_shops:
            print(f"- {shop_name}")
    else:
        print("无未匹配店铺（或所有值全为0）")

    print(result)
    return result


if __name__ == "__main__":
    import os, sys
    from datetime import datetime
    account = os.environ.get("STARTHING_ACCOUNT", "")
    password = os.environ.get("STARTHING_PASSWORD", "")
    if not account or not password:
        print("请设置环境变量 STARTHING_ACCOUNT 和 STARTHING_PASSWORD")
        sys.exit(1)
    if len(sys.argv) >= 3:
        start, end = sys.argv[1], sys.argv[2]
    else:
        d = datetime.now().strftime("%Y-%m-%d")
        start, end = d, d
    data = main(start, end, account, password)
    print(f"\n共返回 {len(data)} 条场地数据")
