# -*- coding: utf-8 -*-
"""
汇联（iotbox.cn）数据采集（原八爪鱼脚本整理）

主入口：main(startTime, endTime, account, password) -> List[Dict]
    输入：起止时间 + 汇联账号密码（参数传入）
    输出：[{"场地": str, "汇联总收入":..., "汇联手续费":..., "汇联现金":..., "汇联实收":..., "汇联非现金":...}]（场地维度，5个指标）

流程：登录(Bearer token)→获取店铺统计列表→逐店查手续费(单独接口)→计算实收/非现金→MySQL huilian列场地映射→返回

注意：汇联实收=总收入-手续费，非现金=实收-现金。一对一兜底匹配。
"""

from decimal import Decimal, InvalidOperation

from utils.mapping import add_unique_mapping
from utils.http import create_retry_session
from utils.mysql_pool import fetch_all_cached
import requests


BASE_URL = "https://merchant.iotbox.cn/wap/api"
TIMEOUT = 30


RESULT_KEYS = [
    "汇联总收入",
    "汇联手续费",
    "汇联现金",
    "汇联实收",
    "汇联非现金",
]


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


def _request_json(session, method, path, **kwargs):
    response = session.request(
        method,
        BASE_URL + path,
        timeout=TIMEOUT,
        **kwargs,
    )
    response.raise_for_status()

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"汇联接口返回的不是JSON：{path}"
        ) from exc

    status = data.get("status")
    if status not in (None, 0, "0"):
        message = data.get("msg") or data.get("message") or "未知错误"
        raise RuntimeError(
            f"汇联接口请求失败：{path}，status={status}，message={message}"
        )

    return data


def _login(session, account, password):
    account = str(account).strip()
    password = str(password).strip()
    if not account or not password:
        raise ValueError("汇联账号和密码不能为空")

    data = _request_json(
        session,
        "POST",
        "/login",
        json={
            "username": account,
            "password": password,
        },
    )
    body = data.get("data") or {}
    token = body.get("token")
    if not token:
        raise RuntimeError("汇联登录成功响应中没有token")

    return str(token)


def _auth_headers(token):
    return {
        "Accept": "application/json, text/plain, */*",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "platform": "",
        "xweb_xhr": "1",
    }


def _get_stats_store_rows(
    session,
    headers,
    start_time,
    end_time,
):
    data = _request_json(
        session,
        "POST",
        "/statsStore/findAllStatsStoreList",
        headers=headers,
        json={
            "dayType": -1,
            "startDateStr": start_time,
            "endDateStr": end_time,
        },
    )
    body = data.get("data") or {}
    rows = body.get("statsStoreVoList") or []
    return [row for row in rows if isinstance(row, dict)]


def _get_service_fee(
    session,
    headers,
    start_time,
    end_time,
    store_id,
):
    data = _request_json(
        session,
        "POST",
        "/statsWriteOffServiceFee/findBillingDetailsData",
        headers=headers,
        json={
            "startTime": start_time,
            "endTime": end_time,
            "merchantStoreId": str(store_id),
            "dayType": "-1",
        },
    )
    body = data.get("data") or {}
    settlement_money = _as_decimal(
        body.get("totalSettlementMoney")
    )
    scan_code_turnover = _as_decimal(
        body.get("totalScanCodePayTotalTurnover")
    )
    return scan_code_turnover - settlement_money


def _get_all_store_data(
    session,
    token,
    start_time,
    end_time,
):
    headers = _auth_headers(token)
    rows = _get_stats_store_rows(
        session,
        headers,
        start_time,
        end_time,
    )
    data_list = []

    for row in rows:
        store_id = row.get("mchStoreId")
        if store_id is None:
            continue

        service_fee = _get_service_fee(
            session,
            headers,
            start_time,
            end_time,
            store_id,
        )
        turnover = _as_decimal(row.get("turnover"))
        cash_money = _as_decimal(row.get("cashMoney"))
        actual_receipt = turnover - service_fee
        non_cash_receipt = actual_receipt - cash_money

        data_list.append(
            {
                "汇联店铺名": str(
                    row.get("storeName") or ""
                ).strip(),
                "汇联总收入": _plain_number(turnover),
                "汇联手续费": _plain_number(service_fee),
                "汇联现金": _plain_number(cash_money),
                "汇联实收": _plain_number(actual_receipt),
                "汇联非现金": _plain_number(non_cash_receipt),
            }
        )

    return data_list


def _get_match_tuples():
    return list(fetch_all_cached(
        "SELECT venue, huilian "
        "FROM company_organizational_structure "
        "WHERE huilian IS NOT NULL;"
    ))


def _build_shop_to_site(match_tuples):
    shop_to_site = {}
    ambiguous = set()
    for site_name, shop_names in match_tuples:
        for shop_name in str(shop_names).splitlines():
            shop_name = shop_name.strip()
            if shop_name:
                add_unique_mapping(shop_to_site, ambiguous, shop_name, site_name, "huilian")
    return shop_to_site


def _matched_item(item, site_name):
    result_item = {"场地": site_name}
    for key in RESULT_KEYS:
        result_item[key] = item.get(key, 0)
    return result_item


def _match_huilian_lists(data_list, match_tuples):
    shop_to_site = _build_shop_to_site(match_tuples)
    result = []
    unmatched_items = []

    for item in data_list:
        shop_name = item.get("汇联店铺名", "")
        site_name = shop_to_site.get(shop_name)
        if site_name is not None:
            result.append(_matched_item(item, site_name))
        else:
            unmatched_items.append(item)

    if (
        not result
        and len(data_list) == 1
        and len(match_tuples) == 1
    ):
        result.append(
            _matched_item(data_list[0], match_tuples[0][0])
        )
        unmatched_items = []

    unmatched_shops = []
    for item in unmatched_items:
        if any(
            _as_decimal(item.get(key)) != 0
            for key in RESULT_KEYS
        ):
            unmatched_shops.append(
                item.get("汇联店铺名", "")
            )

    return result, unmatched_shops


def main(startTime, endTime, account, password):
    start_time = str(startTime).strip()
    end_time = str(endTime).strip()
    if not start_time or not end_time:
        raise ValueError("startTime和endTime不能为空")

    session = create_retry_session()
    session.trust_env = False
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/149.0.0.0 Safari/537.36"
            ),
            "Origin": "https://merchant.iotbox.cn",
            "Referer": "https://merchant.iotbox.cn/wap/",
            "Content-Type": "application/json",
            "platform": "",
        }
    )

    try:
        token = _login(session, account, password)
        data_list = _get_all_store_data(
            session,
            token,
            start_time,
            end_time,
        )
    finally:
        session.close()

    match_tuples = _get_match_tuples()
    result, unmatched_shops = _match_huilian_lists(
        data_list,
        match_tuples,
    )

    print("\n未匹配到的汇联店铺（有值且不全为0）:")
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
    account = os.environ.get("HUILIAN_ACCOUNT", "")
    password = os.environ.get("HUILIAN_PASSWORD", "")
    if not account or not password:
        print("请设置环境变量 HUILIAN_ACCOUNT 和 HUILIAN_PASSWORD")
        sys.exit(1)
    if len(sys.argv) >= 3:
        start, end = sys.argv[1], sys.argv[2]
    else:
        d = datetime.now().strftime("%Y-%m-%d")
        start, end = d, d
    data = main(start, end, account, password)
    print(f"\n共返回 {len(data)} 条场地数据")
