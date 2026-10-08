# -*- coding: utf-8 -*-
"""
多金宝（djb.leyaoyao.com）数据采集（原八爪鱼脚本整理）

主入口：main(start_date, end_date) -> List[Dict]
    输入：起止日期 YYYY-MM-DD
    输出：[{"场地": str, "多金宝现金":..., "多金宝非现金":..., "多金宝手续费":..., "多金宝投币":..., "多金宝出币":..., "多金宝出货":...}, ...]（场地维度，6个指标）

流程：账号登录(ACCOUNTS)→获取店铺列表→切换店铺→拉支付数据+设备数据(5天分段)→MySQL duojinbao列场地映射→聚合返回

注意：
- 账号密码由凭证管理加密保存，运行时注入 ACCOUNTS
- 日期范围自动拆分为5天一段（API限制 split_time_by_five_days）
- 与乐摇摇同体系但不同API域名（djb.leyaoyao.com vs b.leyaoyao.com）
- 核心业务逻辑保持原样，未做功能性改动
"""

import requests
from utils.http import create_retry_session
from utils.mysql_pool import fetch_all_cached
from datetime import datetime, timedelta


BASE_URL = "https://djb.leyaoyao.com"
TIMEOUT = 30

# 账号由适配器从凭证管理页加密存储读取后注入（见 adapters/all_adapters.py DuojinbaoAdapter）
ACCOUNTS = []

SHOP_NAME_ALIASES = {
    "香港柿柿喜物": "香港柿柿喜物总部",
}


def split_time_by_five_days(
    start_date_str,
    end_date_str,
    date_format="%Y-%m-%d",
):
    start_date = datetime.strptime(start_date_str, date_format)
    end_date = datetime.strptime(end_date_str, date_format)

    if start_date > end_date:
        return []

    result = []
    current_start = start_date

    while current_start <= end_date:
        current_end = min(
            current_start + timedelta(days=4),
            end_date,
        )
        result.append(
            [
                current_start.strftime(date_format),
                current_end.strftime(date_format),
            ]
        )
        current_start = current_end + timedelta(days=1)

    return result


def to_float(value):
    if value in (None, ""):
        return 0.0
    return float(value)


def to_int(value):
    if value in (None, ""):
        return 0
    return int(float(value))


def request_json(
    session,
    method,
    path,
    params=None,
    json_data=None,
):
    response = session.request(
        method=method,
        url=BASE_URL + path,
        params=params,
        json=json_data,
        timeout=TIMEOUT,
    )
    response.raise_for_status()

    data = response.json()
    if data.get("code") != 200:
        raise RuntimeError(
            data.get("message")
            or "接口请求失败：{}".format(path)
        )

    return data


def login(session, username, password):
    result = request_json(
        session=session,
        method="POST",
        path="/gw/venue/login",
        json_data={
            "name": str(username).strip(),
            "password": str(password),
        },
    )

    login_data = result.get("data") or {}
    if not login_data.get("user"):
        raise RuntimeError("登录成功，但没有获取到用户信息")


def get_all_stores(session):
    result = request_json(
        session=session,
        method="GET",
        path="/gw/venue/api/v1/merchant/store/staff/merchant",
    )

    merchants = result.get("data") or []
    if isinstance(merchants, dict):
        merchants = [merchants]

    stores = []
    seen_store_ids = set()

    for merchant in merchants:
        merchant_id = merchant.get("merchantId")

        for store in merchant.get("tenantOrgList") or []:
            store_id = store.get("id")
            ad_organization_id = store.get("adOrganizationId")

            if (
                store.get("type") != 2
                or not store_id
                or not ad_organization_id
                or store_id in seen_store_ids
            ):
                continue

            seen_store_ids.add(store_id)
            stores.append(
                {
                    "store_name": store.get("name") or "",
                    "store_id": store_id,
                    "merchant_id": merchant_id,
                    "ad_organization_id": ad_organization_id,
                }
            )

    if not stores:
        raise RuntimeError("登录成功，但没有获取到可访问的店铺")

    return stores


def switch_store(session, store):
    request_json(
        session=session,
        method="GET",
        path=(
            "/gw/venue/api/v1/"
            "merchant/store/staff/resources"
        ),
        params={
            "storeId": store["store_id"],
            "merchantId": store["merchant_id"],
            "adOrganizationId": (
                store["ad_organization_id"]
            ),
        },
    )


def get_payment_data(
    session,
    store_id,
    start_date,
    end_date,
):
    result = request_json(
        session=session,
        method="POST",
        path=(
            "/gw/venue/api/v1/"
            "report/summary/payment"
        ),
        json_data={
            "startOperationTime": start_date.replace("-", ""),
            "endOperationTime": end_date.replace("-", ""),
            "storeIdList": [store_id],
        },
    )

    cash = 0.0
    non_cash = 0.0

    for pay_item in result.get("data") or []:
        pay_name = pay_item.get("payMethodName") or ""
        amount = to_float(
            pay_item.get("subActualAmount")
        )

        if pay_name in ("小程序收款", "在线收款"):
            non_cash += amount
        elif pay_name == "现金收款":
            cash += amount

    return cash, non_cash


def get_device_data(
    session,
    store_id,
    start_date,
    end_date,
):
    result = request_json(
        session=session,
        method="POST",
        path=(
            "/gw/venue/api/v1/"
            "device/screen/data/summary"
        ),
        json_data={
            "summaryStartDate": start_date,
            "summaryEndDate": end_date,
            "contrastStartDate": start_date,
            "contrastEndDate": end_date,
            "storeIdList": [store_id],
            "storeRegionIdList": [],
        },
    )

    summary_data = result.get("data") or {}

    return (
        to_int(summary_data.get("receiveCoinNum")),
        to_int(summary_data.get("sellCoinNum")),
        to_int(summary_data.get("giftNum")),
    )



def get_one_store_data(
    session,
    store,
    date_list,
):
    switch_store(session, store)

    total_cash = 0.0
    total_non_cash = 0.0
    total_receive_coin = 0
    total_sell_coin = 0
    total_gift = 0

    for start_date, end_date in date_list:
        cash, non_cash = get_payment_data(
            session=session,
            store_id=store["store_id"],
            start_date=start_date,
            end_date=end_date,
        )
        receive_coin, sell_coin, gift = get_device_data(
            session=session,
            store_id=store["store_id"],
            start_date=start_date,
            end_date=end_date,
        )

        total_cash += cash
        total_non_cash += non_cash
        total_receive_coin += receive_coin
        total_sell_coin += sell_coin
        total_gift += gift

    return {
        "多金宝店铺名": store["store_name"],
        "多金宝现金": round(total_cash, 2),
        "多金宝非现金": round(total_non_cash, 2),
        "多金宝手续费": round(total_non_cash * 0.006, 2),
        "多金宝投币": total_receive_coin,
        "多金宝出币": total_sell_coin,
        "多金宝出货": total_gift,
    }


def create_session():
    session = create_retry_session()
    session.trust_env = False
    session.headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Origin": BASE_URL,
            "Referer": (
                BASE_URL
                + "/pages/smallground_b/index.html"
            ),
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        }
    )
    return session


def get_match_list():
    return list(fetch_all_cached(
        """
        SELECT venue, duojinbao
        FROM company_organizational_structure
        WHERE duojinbao IS NOT NULL
        """
    ))


def match_duojinbao_lists(data_list, match_list):
    result = []
    unmatched_shops = []
    shop_to_site = {
        str(shop_name).strip(): site_name
        for site_name, shop_name in match_list
    }

    for item in data_list:
        shop_name = str(
            item.get("多金宝店铺名", "") or ""
        ).strip()
        match_shop_name = SHOP_NAME_ALIASES.get(
            shop_name,
            shop_name,
        )

        if match_shop_name in shop_to_site:
            new_dict = {
                "场地": shop_to_site[match_shop_name]
            }
            for key, value in item.items():
                if key != "多金宝店铺名":
                    new_dict[key] = value
            result.append(new_dict)
            continue

        has_non_zero = any(
            isinstance(value, (int, float))
            and value != 0
            for key, value in item.items()
            if key != "多金宝店铺名"
        )
        if has_non_zero:
            unmatched_shops.append(shop_name)

    return result, unmatched_shops


def main(start_date, end_date):
    start_date = str(start_date).strip()
    end_date = str(end_date).strip()
    date_list = split_time_by_five_days(
        start_date,
        end_date,
    )

    if not date_list:
        raise ValueError("开始日期不能晚于结束日期")
    if not ACCOUNTS:
        raise RuntimeError(
            "[多金宝] 未配置账号，请在凭证管理页填写采集账号和采集密码"
        )

    result = []
    processed_store_keys = set()

    for account in ACCOUNTS:
        session = create_session()

        try:
            login(
                session=session,
                username=account["username"],
                password=account["password"],
            )
            stores = get_all_stores(session)

            for store in stores:
                store_key = (
                    store["merchant_id"],
                    store["store_id"],
                )

                if store_key in processed_store_keys:
                    continue

                result.append(
                    get_one_store_data(
                        session=session,
                        store=store,
                        date_list=date_list,
                    )
                )
                processed_store_keys.add(store_key)
        finally:
            session.close()

    match_list = get_match_list()
    result, unmatched_shops = match_duojinbao_lists(
        result,
        match_list,
    )

    print(
        "\n未匹配到的店铺（有值且不全为0）:",
        end="",
    )
    if unmatched_shops:
        print("{}个".format(len(unmatched_shops)))
        for shop_name in unmatched_shops:
            print("- {}".format(shop_name))
    else:
        print("无未匹配店铺（或所有值全为0）")

    print(result)
    return result


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        start, end = sys.argv[1], sys.argv[2]
    else:
        d = datetime.now().strftime("%Y-%m-%d")
        start, end = d, d
    data = main(start, end)
    print(f"\n共返回 {len(data)} 条场地数据")
