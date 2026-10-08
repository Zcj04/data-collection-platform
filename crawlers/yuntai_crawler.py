# -*- coding: utf-8 -*-
"""
芸苔（rapa.vip）数据采集（原八爪鱼脚本整理）

主入口：main(start_time, end_time, account, password) -> List[Dict]
    输入：起止时间（支持 YYYY-MM-DD / YYYY-MM-DD HH:MM:SS / 毫秒时间戳）+ 芸苔账号密码
    输出：[{"场地": str, "芸苔现金":..., "芸苔微信":..., ..., "芸苔积分减少":...}, ...]（场地维度，11个指标）

流程：登录获取Token(可能多主体) -> 获取租户列表 -> 拉取收入/跨场地结算/设备数据/积分 -> 按场地映射聚合

注意：
- account/password 通过参数传入（规范做法），适配器从凭证管理读取注入
- DB_CONFIG 为模块级常量（密码明文），阶段4 移入加密配置
- 依赖 MySQL store_mapping_db 库（company_organizational_structure.yuntai/yuntai2 场地映射）
- 核心业务逻辑保持原样，未做功能性改动
"""

import base64
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import requests

from utils.mapping import add_unique_mapping
from utils.http import create_retry_session
from utils.mysql_pool import fetch_all_cached


BASE_URL = "https://www.rapa.vip"
TIMEOUT = 60
CHINA_TZ = timezone(timedelta(hours=8))

LOGIN_PATH = "/Identity/api/v1/Account/Login"
LOGIN_BY_ID_PATH = "/Identity/api/v1/Account/LoginById"
STATISTICS_BASE = (
    "/yunyoutenant/api/v1/MarketingStatistic"
)

RESULT_KEYS = [
    "芸苔现金",
    "芸苔微信",
    "芸苔支付宝",
    "芸苔手续费",
    "芸苔非团购",
    "芸苔远程取币",
    "芸苔投币",
    "芸苔出货",
    "芸苔出币",
    "芸苔积分增加",
    "芸苔积分减少",
]


class YuntaiApiError(RuntimeError):
    def __init__(self, path, code, message):
        self.path = path
        self.code = str(code)
        self.message = str(message or "未知错误")
        super().__init__(
            f"芸苔接口请求失败：{path}，"
            f"code={self.code}，message={self.message}"
        )


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


def _to_timestamp_ms(value, is_end):
    if isinstance(value, (int, float)):
        number = int(value)
        return number * 1000 if number < 10**11 else number

    text = str(value).strip()
    if not text:
        raise ValueError("时间参数不能为空")
    if text.isdigit():
        number = int(text)
        return number * 1000 if number < 10**11 else number

    date_only = len(text) == 10
    formats = (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    )
    parsed = None
    for date_format in formats:
        try:
            parsed = datetime.strptime(text[:19], date_format)
            break
        except ValueError:
            continue

    if parsed is None:
        raise ValueError(
            "时间格式必须为YYYY-MM-DD、"
            "YYYY-MM-DD HH:MM:SS或毫秒时间戳"
        )

    parsed = parsed.replace(tzinfo=CHINA_TZ)
    if is_end and date_only:
        parsed += timedelta(days=1)
        return int(parsed.timestamp() * 1000) - 1
    return int(parsed.timestamp() * 1000)


def _request_data(
    session,
    method,
    path,
    token=None,
    **kwargs,
):
    headers = dict(kwargs.pop("headers", {}) or {})
    if token:
        headers["Authorization"] = token

    response = session.request(
        method,
        BASE_URL + path,
        headers=headers,
        timeout=TIMEOUT,
        **kwargs,
    )
    response.raise_for_status()

    try:
        response_json = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"芸苔接口返回的不是JSON：{path}"
        ) from exc

    status = response_json.get("ResponseStatus") or {}
    code = status.get("ErrorCode")
    if str(code) != "0":
        raise YuntaiApiError(
            path,
            code,
            status.get("Message"),
        )

    return response_json.get("Data")


def _login(session, account, password):
    account = str(account).strip()
    password = str(password).strip()
    if not account or not password:
        raise ValueError("芸苔账号和密码不能为空")

    encoded_password = base64.b64encode(
        password.encode("utf-8")
    ).decode("ascii")
    payload = {
        "Account": account,
        "Password": encoded_password,
        "ImgCode": "",
        "ImgCodeID": "",
        "openId": None,
    }

    try:
        login_data = _request_data(
            session,
            "POST",
            LOGIN_PATH,
            json=payload,
        ) or {}
    except YuntaiApiError as exc:
        if exc.code in {
            "2009000002",
            "2023000003",
            "2009000011",
        }:
            raise RuntimeError(
                "芸苔登录需要图形验证码，请先在官网完成一次验证"
            ) from exc
        raise

    token = login_data.get("Token")
    if not token:
        raise RuntimeError("芸苔登录成功响应中没有Token")
    if login_data.get("FirstLogin"):
        raise RuntimeError(
            "芸苔账号是首次登录，请先在官网修改初始密码"
        )

    merchants = login_data.get("MerchantList") or []
    if len(merchants) <= 1:
        return [str(token)]

    tokens = []
    for merchant in merchants:
        customer_id = merchant.get("CustomerID")
        if not customer_id:
            continue

        selected = _request_data(
            session,
            "POST",
            LOGIN_BY_ID_PATH,
            token=str(token),
            json={
                "Phone": account,
                "CustomerID": customer_id,
                "OpenID": None,
            },
        ) or {}
        selected_token = selected.get("Token")
        if selected_token:
            tokens.append(str(selected_token))

    if not tokens:
        raise RuntimeError("芸苔账号下没有可访问的主体")
    return tokens


def _get_tenants(session, token):
    body = _request_data(
        session,
        "GET",
        f"{STATISTICS_BASE}/GetTenantList",
        token=token,
        params={
            "PageIndex": 1,
            "PageSize": 5000,
            "IsDel": "false",
        },
    ) or {}
    rows = body.get("Data") or []
    return [
        {
            "id": str(row.get("ID")),
            "name": str(row.get("Name") or "").strip(),
        }
        for row in rows
        if row.get("ID")
    ]


def _new_metric_row(tenant):
    row = {
        "tenant_id": tenant["id"],
        "芸苔店铺名": tenant["name"],
    }
    for key in RESULT_KEYS:
        row[key] = Decimal("0")
    return row


def _find_tenant_id(name, name_to_id):
    name = str(name or "").strip()
    if name in name_to_id:
        return name_to_id[name]

    matches = [
        tenant_name
        for tenant_name in name_to_id
        if tenant_name and tenant_name in name
    ]
    if not matches:
        return None
    return name_to_id[max(matches, key=len)]


def _fetch_pages(
    session,
    token,
    path,
    base_payload,
    list_key,
):
    result = []
    page_index = 1

    while True:
        payload = {
            **base_payload,
            "PageIndex": page_index,
        }
        body = _request_data(
            session,
            "POST",
            path,
            token=token,
            json=payload,
        ) or {}
        rows = body.get(list_key) or []
        if not rows:
            break

        result.extend(rows)
        total = body.get("Total")
        if total is not None and len(result) >= int(total):
            break
        if len(rows) < int(payload.get("PageSize", 100)):
            break

        page_index += 1

    return result


def _fill_income(
    session,
    token,
    metrics,
    tenant_ids,
    start_ms,
    end_ms,
):
    body = _request_data(
        session,
        "POST",
        f"{STATISTICS_BASE}/GetTenantAnalysisDetail",
        token=token,
        json={
            "TenantIDs": tenant_ids,
            "StartDate": start_ms,
            "EndDate": end_ms,
            "PageIndex": 1,
            "PageSize": 5000,
            "IsShowPercent": True,
            "Sort": "",
            "SortType": "",
        },
    ) or {}

    for tenant in body.get("Data") or []:
        tenant_id = str(tenant.get("TenantID") or "")
        row = metrics.get(tenant_id)
        if row is None:
            continue

        for payment in tenant.get("IncomeData") or []:
            payment_name = payment.get("PaymentName")
            income = _as_decimal(payment.get("Income"))
            if payment_name == "现金":
                row["芸苔现金"] += income
            elif payment_name == "微信":
                row["芸苔微信"] += income
            elif payment_name == "支付宝":
                row["芸苔支付宝"] += income
        row["芸苔手续费"] = (
            row["芸苔微信"]
            + row["芸苔支付宝"]
        ) * Decimal("0.006")

        row["芸苔非团购"] = (
            row["芸苔现金"]
            + row["芸苔微信"]
            + row["芸苔支付宝"]
        )


def _fill_across_settlement(
    session,
    token,
    metrics,
    start_ms,
    end_ms,
):
    body = _request_data(
        session,
        "POST",
        f"{STATISTICS_BASE}/GetAcrossSettlementList",
        token=token,
        json={
            "TenantID": (
                "00000000-0000-0000-0000-000000000000"
            ),
            "StartTime": start_ms,
            "EndTime": end_ms,
        },
    ) or {}

    for item in body.get("Data") or []:
        tenant_name = str(
            item.get("TenantName") or ""
        ).strip()
        if tenant_name in "福州A广场":
            continue

        tenant_id = str(item.get("TenantID") or "")
        row = metrics.get(tenant_id)
        if row is not None:
            row["芸苔远程取币"] = _as_decimal(
                item.get("TotalMoney")
            )


def _fill_device_data(
    session,
    token,
    metrics,
    tenants,
    tenant_ids,
    start_ms,
    end_ms,
):
    name_to_id = {
        tenant["name"]: tenant["id"]
        for tenant in tenants
    }
    common_payload = {
        "TenantIDs": tenant_ids,
        "StartDate": start_ms,
        "EndDate": end_ms,
        "PageSize": 100,
        "Sort": "",
        "SortType": "",
    }

    game_rows = _fetch_pages(
        session,
        token,
        f"{STATISTICS_BASE}/GetDeviceAnalysisDetail",
        {
            **common_payload,
            "DeviceType": "GameMachine",
        },
        "Data",
    )
    for item in game_rows:
        tenant_id = _find_tenant_id(
            item.get("BelongToTenant"),
            name_to_id,
        )
        row = metrics.get(tenant_id)
        if row is None:
            continue
        row["芸苔投币"] += (
            _as_decimal(item.get("OnlineCoinIn"))
            + _as_decimal(item.get("OfflineCoinIn"))
        )
        row["芸苔出货"] += _as_decimal(
            item.get("GiftConsumption")
        )

    coin_rows = _fetch_pages(
        session,
        token,
        f"{STATISTICS_BASE}/GetDeviceAnalysisDetail",
        {
            **common_payload,
            "DeviceType": "CoinExchange",
        },
        "CoinExchangeData",
    )
    for item in coin_rows:
        tenant_id = _find_tenant_id(
            item.get("BelongToTenant"),
            name_to_id,
        )
        row = metrics.get(tenant_id)
        if row is not None:
            row["芸苔出币"] += _as_decimal(
                item.get("ActualCoinAmount")
            )


def _fill_points(
    session,
    token,
    metrics,
    tenant_ids,
    start_ms,
    end_ms,
):
    for tenant_id in tenant_ids:
        row = metrics[tenant_id]
        for method in ("Recovery", "Exchange"):
            items = _fetch_pages(
                session,
                token,
                f"{STATISTICS_BASE}/GetGoodsAnalysisDetail",
                {
                    "TenantIDs": [tenant_id],
                    "StartDate": start_ms,
                    "EndDate": end_ms,
                    "PageSize": 100,
                    "GoodsKind": "",
                    "ConsumptionMethod": method,
                    "Sort": "",
                    "SortType": "",
                },
                "Data",
            )
            for item in items:
                if method == "Recovery":
                    row["芸苔积分增加"] += (
                        _as_decimal(
                            item.get("RecoveryAmount")
                        )
                        - _as_decimal(
                            item.get("CancelRecoveryAmount")
                        )
                    )
                else:
                    row["芸苔积分减少"] += _as_decimal(
                        item.get("Integral")
                    )


def _collect_token_data(
    session,
    token,
    start_ms,
    end_ms,
):
    tenants = _get_tenants(session, token)
    metrics = {
        tenant["id"]: _new_metric_row(tenant)
        for tenant in tenants
    }
    tenant_ids = list(metrics)
    if not tenant_ids:
        return []

    _fill_income(
        session,
        token,
        metrics,
        tenant_ids,
        start_ms,
        end_ms,
    )
    _fill_across_settlement(
        session,
        token,
        metrics,
        start_ms,
        end_ms,
    )
    _fill_device_data(
        session,
        token,
        metrics,
        tenants,
        tenant_ids,
        start_ms,
        end_ms,
    )
    _fill_points(
        session,
        token,
        metrics,
        tenant_ids,
        start_ms,
        end_ms,
    )

    rows = []
    for row in metrics.values():
        output_row = {
            "tenant_id": row["tenant_id"],
            "芸苔店铺名": row["芸苔店铺名"],
        }
        for key in RESULT_KEYS:
            output_row[key] = _plain_number(row[key])
        rows.append(output_row)
    return rows


def _get_match_tuples():
    return list(fetch_all_cached(
        "SELECT venue, yuntai,yuntai2 "
        "FROM company_organizational_structure "
        "WHERE yuntai IS NOT NULL or yuntai2 IS NOT NULL;"
    ))


def _match_yuntai_lists(data_list, match_tuples):
    shop_to_site = {}
    ambiguous = set()

    # match_tuples 每行三个元素：(venue, yuntai, yuntai2)
    for row in match_tuples:
        site_name = row[0]
        col1 = row[1]  # yuntai
        col2 = row[2]  # yuntai2

        # 处理 yuntai
        if col1 is not None:
            for shop_name in str(col1).splitlines():
                shop_name = shop_name.strip()
                if shop_name:
                    add_unique_mapping(shop_to_site, ambiguous, shop_name, site_name, "yuntai")

        # 处理 yuntai2
        if col2 is not None:
            for shop_name in str(col2).splitlines():
                shop_name = shop_name.strip()
                if shop_name:
                    add_unique_mapping(shop_to_site, ambiguous, shop_name, site_name, "yuntai")

    # 按场地聚合
    site_agg = {}
    unmatched_shops = []

    for item in data_list:
        shop_name = item.get("芸苔店铺名", "")
        site_name = shop_to_site.get(shop_name)
        if site_name is not None:
            if site_name not in site_agg:
                site_agg[site_name] = {"场地": site_name}
                for key in RESULT_KEYS:
                    site_agg[site_name][key] = 0
            # 累加所有指标
            for key in RESULT_KEYS:
                site_agg[site_name][key] += item.get(key, 0)
        else:
            # 未匹配且有数据则记录
            if any(_as_decimal(item.get(key)) != 0 for key in RESULT_KEYS):
                unmatched_shops.append(shop_name)

    result = list(site_agg.values())
    return result, unmatched_shops


def main(start_time, end_time, account, password):
    start_ms = _to_timestamp_ms(start_time, is_end=False)
    end_ms = _to_timestamp_ms(end_time, is_end=True)
    if start_ms > end_ms:
        raise ValueError("start_time不能晚于end_time")

    session = create_retry_session()
    session.trust_env = False
    session.headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/149.0.0.0 Safari/537.36"
            ),
            "Origin": BASE_URL,
            "Referer": (
                BASE_URL + "/appyunyoutenant/"
            ),
        }
    )

    try:
        tokens = _login(
            session,
            account,
            password,
        )
        all_rows = []
        seen_tenant_ids = set()
        for token in tokens:
            for row in _collect_token_data(
                session,
                token,
                start_ms,
                end_ms,
            ):
                tenant_id = row["tenant_id"]
                if tenant_id in seen_tenant_ids:
                    continue
                seen_tenant_ids.add(tenant_id)
                all_rows.append(row)
    finally:
        session.close()

    match_tuples = _get_match_tuples()
    result, unmatched_shops = _match_yuntai_lists(
        all_rows,
        match_tuples,
    )

    print("\n未匹配到的芸苔店铺（有值且不全为0）:")
    if unmatched_shops:
        for shop_name in unmatched_shops:
            print(f"- {shop_name}")
    else:
        print("无未匹配店铺（或所有值全为0）")

    print(result)
    return result


if __name__ == "__main__":
    # 单独运行测试：python crawlers/yuntai_crawler.py [start] [end]
    # 账号密码从环境变量读取（YUNTAI_ACCOUNT / YUNTAI_PASSWORD），不写代码里
    import os
    import sys
    account = os.environ.get("YUNTAI_ACCOUNT", "")
    password = os.environ.get("YUNTAI_PASSWORD", "")
    if not account or not password:
        print("请设置环境变量 YUNTAI_ACCOUNT 和 YUNTAI_PASSWORD")
        sys.exit(1)
    if len(sys.argv) >= 3:
        start, end = sys.argv[1], sys.argv[2]
    else:
        d = datetime.now().strftime("%Y-%m-%d")
        start, end = d, d
    data = main(start, end, account, password)
    print(f"\n共返回 {len(data)} 条场地数据")
