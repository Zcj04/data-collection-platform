# -*- coding: utf-8 -*-
"""
新系统（CloudBase 云开发）数据采集（原八爪鱼脚本整理）

主入口：main(start_date, end_date, account, password) -> List[Dict]
    输入：起止日期 YYYY-MM-DD + 新系统账号密码（参数传入）
    输出：[{"场地": str, "新系统积分增加":..., "新系统积分减少":...}]（场地维度，2个指标）

流程：
  统计模块(直接查店铺积分/增加+减少)→CloudBase HMAC-SHA256登录→
  查询已核销抵扣券→多策略场地匹配(精确/模糊/商品订单/抵扣券订单)→汇总

特殊：
- 双API体系：公共API(直接请求) + CloudBase API(HMAC-SHA256签名)
- 多层级场地匹配：精确→模糊→通过手机号查历史商品订单→抵扣券订单
- 4次重试机制(指数退避)
- CLOUDBASE_CONFIG 含 access_key/secret_key 硬编码（阶段4外置）
- 核心业务逻辑保持原样，未做功能性改动
"""

from collections import OrderedDict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import os
import re
import time
import uuid

from core.config import load_env
import requests
from utils.http import create_retry_session
from utils.mysql_pool import fetch_all_cached

# 确保 .env 已加载（密钥只从环境变量/凭证管理读取，禁止硬编码默认值）
load_env()


PUBLIC_API_URL = (
    "https://env-00jxtojvu5lg.dev-hz.cloudbasefunction.cn/a_src"
)
CLOUDBASE_API_URL = (
    "https://env-00jxtojvu5lg.api-hz.cloudbasefunction.cn"
    "/functions/invokeFunction"
)
TIMEOUT = 60
REQUEST_RETRIES = 4
RETRY_BASE_DELAY = 1

# 密钥只从环境变量/凭证管理读取（space_id/space_app_id 为公开标识，可放代码）
# 凭证管理页字段：access_key / secret_key
CLOUDBASE_CONFIG = {
    "space_id": os.getenv(
        "NEWSYSTEM_SPACE_ID",
        "env-00jxtojvu5lg",
    ),
    "space_app_id": os.getenv(
        "NEWSYSTEM_SPACE_APP_ID",
        "2021004153602597",
    ),
    "access_key": os.getenv("NEWSYSTEM_ACCESS_KEY", ""),
    "secret_key": os.getenv("NEWSYSTEM_SECRET_KEY", ""),
}


MAX_UNMATCHED_LOGS = 20


def _as_decimal(value):
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _mask_sensitive(value) -> str:
    """日志中仅保留手机号等账号的首尾少量字符。"""
    text = str(value or "")
    if len(text) <= 4:
        return "***" if text else ""
    return text[:2] + "***" + text[-2:]


def _plain_number(value):
    number = _as_decimal(value)
    if number == number.to_integral_value():
        return int(number)
    return float(number)


def _normalize_date(value, field_name):
    text = str(value).strip()
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d")
    except ValueError as error:
        raise ValueError(
            "{}格式必须为YYYY-MM-DD".format(field_name)
        ) from error


def _normalize_text(value):
    return re.sub(
        r"[\s（）()【】\[\]_\-]+",
        "",
        str(value or "").strip().lower(),
    )


def _split_shop_names(shop_names):
    return [
        shop_name.strip()
        for shop_name in str(shop_names).splitlines()
        if shop_name.strip()
    ]


def _get_match_tuples():
    return list(fetch_all_cached(
        "SELECT venue, new_system "
        "FROM company_organizational_structure "
        "WHERE new_system IS NOT NULL;"
    ))


def _build_shop_mapping(match_tuples):
    mapping = []
    for venue, shop_names in match_tuples:
        for shop_name in _split_shop_names(shop_names):
            mapping.append(
                {
                    "venue": venue,
                    "shop_name": shop_name,
                    "normalized": _normalize_text(shop_name),
                }
            )

    mapping.sort(
        key=lambda item: len(item["normalized"]),
        reverse=True,
    )
    return mapping


def _match_venue(shop_value, shop_mapping):
    shop_text = str(shop_value or "").strip()
    if not shop_text:
        return None

    candidates = [shop_text]
    primary_name = re.split(
        r"[,，|｜]",
        shop_text,
        maxsplit=1,
    )[0].strip()
    if primary_name and primary_name != shop_text:
        candidates.append(primary_name)

    for candidate in candidates:
        normalized_value = _normalize_text(candidate)
        if not normalized_value:
            continue

        for item in shop_mapping:
            normalized_shop = item["normalized"]
            if normalized_value == normalized_shop:
                return item["venue"]

        for item in shop_mapping:
            normalized_shop = item["normalized"]
            if len(normalized_shop) < 4:
                continue
            if (
                normalized_shop in normalized_value
                or normalized_value in normalized_shop
            ):
                return item["venue"]

    return None


def _request_shop_points(
    session,
    shop_name,
    start_date,
    exclusive_end_date,
):
    payload = {
        "action": "statistics",
        "data": {
            "shop_name": shop_name,
            "start_date": start_date,
            "end_date": exclusive_end_date,
            "active_tab": "",
            "date": datetime.now().strftime("%Y-%m-%d"),
        },
    }
    last_error = None

    for attempt in range(REQUEST_RETRIES):
        try:
            response = session.post(
                PUBLIC_API_URL,
                json=payload,
                timeout=TIMEOUT,
            )
            response.raise_for_status()

            try:
                response_json = response.json()
            except ValueError as error:
                raise RuntimeError(
                    "新系统接口返回的不是JSON：{}".format(
                        shop_name
                    )
                ) from error

            api_data = response_json.get("data") or {}
            code = api_data.get("code")
            if code not in (200, "200"):
                raise RuntimeError(
                    "新系统店铺查询失败：{}，code={}，"
                    "message={}".format(
                        shop_name,
                        code,
                        api_data.get("msg") or "未知错误",
                    )
                )

            point_data = api_data.get("data") or {}
            return (
                _as_decimal(point_data.get("add_total")),
                _as_decimal(point_data.get("subtract_total")),
            )
        except Exception as error:
            last_error = error
            if attempt + 1 >= REQUEST_RETRIES:
                break
            time.sleep(RETRY_BASE_DELAY * (2 ** attempt))

    raise RuntimeError(
        "新系统店铺查询重试{}次后仍失败：{}，{}".format(
            REQUEST_RETRIES,
            shop_name,
            last_error,
        )
    )


def _export_point_records(
    session,
    start_time,
    end_time,
):
    payload = {
        "action": "export_point",
        "data": {
            "startDate": start_time,
            "endDate": end_time,
        },
    }
    last_error = None

    for attempt in range(REQUEST_RETRIES):
        try:
            response = session.post(
                PUBLIC_API_URL,
                json=payload,
                timeout=TIMEOUT,
            )
            response.raise_for_status()
            response_json = response.json()
            api_data = response_json.get("data") or {}

            if api_data.get("code") not in (200, "200"):
                raise RuntimeError(
                    "code={}，message={}".format(
                        api_data.get("code"),
                        api_data.get("message")
                        or api_data.get("msg")
                        or "未知错误",
                    )
                )

            detail_data = api_data.get("data") or {}
            return detail_data.get("list") or []
        except Exception as error:
            last_error = error
            if attempt + 1 >= REQUEST_RETRIES:
                break
            time.sleep(RETRY_BASE_DELAY * (2 ** attempt))

    raise RuntimeError(
        "积分明细导出重试{}次后仍失败：{}".format(
            REQUEST_RETRIES,
            last_error,
        )
    )


class CloudBaseClient:
    def __init__(self, session):
        self.session = session
        self.token = ""

    @staticmethod
    def _client_info():
        device_id = os.getenv("NEW_SYSTEM_DEVICEID", "")
        app_id = os.getenv("NEW_SYSTEM_APPID", "")
        return {
            "PLATFORM": "web",
            "OS": "windows",
            "APPID": app_id,
            "DEVICEID": device_id,
            "scene": 1001,
            "appId": app_id,
            "appLanguage": "zh-Hans",
            "appName": "CMS-01",
            "appVersion": "1.0.0",
            "appVersionCode": "100",
            "browserName": "chrome",
            "browserVersion": "148.0.0.0",
            "deviceId": device_id,
            "deviceModel": "PC",
            "deviceType": "pc",
            "hostName": "chrome",
            "hostVersion": "148.0.0.0",
            "osName": "windows",
            "osVersion": "10 x64",
            "uniCompilerVersion": "5.03",
            "uniPlatform": "web",
            "uniRuntimeVersion": "5.03",
            "locale": "zh-Hans",
            "LOCALE": "zh-Hans",
        }

    @staticmethod
    def _sha256_hex(value):
        return hashlib.sha256(
            value.encode("utf-8")
        ).hexdigest()

    def _signed_headers(self, function_name, body_text):
        timestamp = str(int(time.time() * 1000))
        request_id = str(uuid.uuid4())
        config = CLOUDBASE_CONFIG
        access_key = config.get("access_key") or ""
        secret_key = config.get("secret_key") or ""
        if not access_key or not secret_key:
            raise RuntimeError(
                "未配置新系统 CloudBase 密钥：请设置环境变量 "
                "NEWSYSTEM_ACCESS_KEY / NEWSYSTEM_SECRET_KEY，"
                "或在凭证管理页填写 access_key / secret_key"
            )

        headers = {
            "x-to-function-name": function_name,
            "x-from-app-id": config["space_app_id"],
            "x-from-env-id": config["space_id"],
            "x-to-env-id": config["space_id"],
            "x-from-instance-id": timestamp,
            "x-from-function-name": function_name,
            "x-client-timestamp": timestamp,
            "x-alipay-source": "client",
            "x-request-id": request_id,
            "x-alipay-callid": request_id,
            "x-trace-id": request_id,
        }

        signed_header_names = sorted(
            [
                "x-from-app-id",
                "x-from-env-id",
                "x-to-env-id",
                "x-from-instance-id",
                "x-from-function-name",
                "x-client-timestamp",
                "x-to-function-name",
            ]
        )
        signed_headers = ";".join(signed_header_names)
        canonical_headers = "".join(
            "{}:{}\n".format(name, headers[name])
            for name in signed_header_names
        )
        canonical_request = (
            "POST\n"
            "/functions/invokeFunction\n"
            "\n"
            "{}\n"
            "{}\n"
            "{}\n"
        ).format(
            canonical_headers,
            signed_headers,
            self._sha256_hex(body_text),
        )
        string_to_sign = (
            "HMAC-SHA256\n{}\n{}\n"
        ).format(
            timestamp,
            self._sha256_hex(canonical_request),
        )
        signature = hmac.new(
            config["secret_key"].encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        headers.update(
            {
                "Authorization": (
                    "HMAC-SHA256 Credential={}, "
                    "SignedHeaders={}, Signature={}"
                ).format(
                    access_key,
                    signed_headers,
                    signature,
                ),
                "Accept": "*/*",
                "Content-Type": "application/json",
                "Origin": (
                    "https://env-00jxtojvu5lg-static"
                    ".normal.cloudstatic.cn"
                ),
                "Referer": (
                    "https://env-00jxtojvu5lg-static"
                    ".normal.cloudstatic.cn/admin/"
                ),
            }
        )
        return headers

    def invoke(self, function_name, data, include_token=True):
        payload = dict(data)
        payload["clientInfo"] = self._client_info()
        if include_token and self.token:
            payload["uniIdToken"] = self.token

        body_text = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        response = self.session.post(
            CLOUDBASE_API_URL,
            headers=self._signed_headers(
                function_name,
                body_text,
            ),
            data=body_text.encode("utf-8"),
            timeout=TIMEOUT,
        )

        try:
            response_json = response.json()
        except ValueError as error:
            raise RuntimeError(
                "云开发接口返回的不是JSON：HTTP {}，{}".format(
                    response.status_code,
                    response.text[:300],
                )
            ) from error

        if response.status_code >= 400:
            raise RuntimeError(
                "云开发接口请求失败：HTTP {}，{}".format(
                    response.status_code,
                    response_json.get("errMsg")
                    or response_json.get("message")
                    or response_json.get("errDetail")
                    or "",
                )
            )

        new_token = response_json.get("newToken") or {}
        refreshed_token = (
            new_token.get("token")
            if isinstance(new_token, dict)
            else ""
        )
        if refreshed_token:
            self.token = refreshed_token

        return response_json

    def login(self, account, password):
        account = str(account).strip()
        login_params = {
            "password": str(password),
            "captcha": "",
        }

        if re.fullmatch(r"1\d{10}", account):
            login_params["mobile"] = account
        elif "@" in account:
            login_params["email"] = account
        else:
            login_params["username"] = account

        result = self.invoke(
            "uni-id-co",
            {
                "method": "login",
                "params": [login_params],
            },
            include_token=False,
        )

        err_code = result.get("errCode")
        if err_code not in (None, 0, "0"):
            raise RuntimeError(
                "新系统登录失败：errCode={}，errMsg={}".format(
                    err_code,
                    result.get("errMsg") or "",
                )
            )

        new_token = result.get("newToken") or {}
        token = (
            new_token.get("token")
            if isinstance(new_token, dict)
            else ""
        )
        token = token or result.get("token") or ""
        if not token:
            raise RuntimeError(
                "新系统登录成功但未返回uniIdToken"
            )

        self.token = token
        return result

    def _query_records_by_where(
        self,
        collection_name,
        field_names,
        order_field,
        where_data,
    ):
        all_rows = []
        page_size = 100
        skip = 0
        dollar = "$"

        while True:
            command = [
                {
                    dollar + "method": "collection",
                    dollar + "param": [collection_name],
                },
                {
                    dollar + "method": "where",
                    dollar + "param": [where_data],
                },
                {
                    dollar + "method": "field",
                    dollar + "param": [field_names],
                },
                {
                    dollar + "method": "orderBy",
                    dollar + "param": [order_field, "desc"],
                },
                {
                    dollar + "method": "skip",
                    dollar + "param": [skip],
                },
                {
                    dollar + "method": "limit",
                    dollar + "param": [page_size],
                },
                {
                    dollar + "method": "get",
                    dollar + "param": [{"getCount": True}],
                },
            ]
            result = self.invoke(
                "DCloud-clientDB",
                {
                    "command": {
                        dollar + "db": command
                    }
                },
            )

            code = result.get("code")
            if code not in (None, 0, "0"):
                raise RuntimeError(
                    "查询{}失败："
                    "code={}，message={}".format(
                        collection_name,
                        code,
                        result.get("message") or "",
                    )
                )

            rows = result.get("data") or []
            if isinstance(rows, dict):
                rows = rows.get("data") or []
            all_rows.extend(rows)

            count = result.get("count")
            if count is None:
                count = len(all_rows)

            if (
                len(rows) < page_size
                or len(all_rows) >= int(count)
            ):
                break
            skip += page_size

        return all_rows

    def query_verified_coupon_orders(
        self,
        start_time,
        end_time,
    ):
        dollar = "$"
        start_command = {
            dollar + "db": [
                {
                    dollar + "method": "command"
                },
                {
                    dollar + "method": "gte",
                    dollar + "param": [start_time],
                },
                {
                    dollar + "method": "and",
                    dollar + "param": [
                        {
                            dollar + "db": [
                                {
                                    dollar + "method": "command"
                                },
                                {
                                    dollar + "method": "lte",
                                    dollar + "param": [end_time],
                                },
                            ]
                        }
                    ],
                },
            ]
        }
        return self._query_records_by_where(
            "a_coupon_order",
            (
                "coupon_title,point_cost,user_phone,status,"
                "exchange_time,verify_time,verify_shop"
            ),
            "verify_time",
            {
                "status": "已核销",
                "verify_time": start_command,
            },
        )

    def _query_records_for_phones(
        self,
        collection_name,
        field_names,
        order_field,
        phones,
        batch_size=50,
    ):
        normalized_phones = sorted(
            set(
                str(phone).strip()
                for phone in phones
                if str(phone).strip()
            )
        )
        records_by_phone = {
            phone: []
            for phone in normalized_phones
        }
        dollar = "$"

        for index in range(
            0,
            len(normalized_phones),
            batch_size,
        ):
            phone_batch = normalized_phones[
                index:index + batch_size
            ]
            phone_condition = {
                dollar + "db": [
                    {
                        dollar + "method": "command"
                    },
                    {
                        dollar + "method": "in",
                        dollar + "param": [phone_batch],
                    },
                ]
            }
            rows = self._query_records_by_where(
                collection_name,
                field_names,
                order_field,
                {"user_phone": phone_condition},
            )

            for row in rows:
                phone = str(
                    row.get("user_phone") or ""
                ).strip()
                if phone in records_by_phone:
                    records_by_phone[phone].append(row)

        return records_by_phone

    def query_product_orders_for_phones(self, phones):
        return self._query_records_for_phones(
            "a_order",
            (
                "goods_name,shop_mess,pay_time,status,"
                "user_phone,total_price,delivery_type"
            ),
            "pay_time",
            phones,
        )

    def query_coupon_orders_for_phones(self, phones):
        return self._query_records_for_phones(
            "a_coupon_order",
            (
                "coupon_title,point_cost,user_phone,status,"
                "exchange_time,verify_time,verify_shop"
            ),
            "exchange_time",
            phones,
        )


def _parse_datetime(value):
    if isinstance(value, datetime):
        return value

    text = str(value or "").strip()
    for date_format in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text[:19], date_format)
        except ValueError:
            pass
    return None


def _point_subject(point_record):
    content = str(
        point_record.get("projectContent") or ""
    ).strip()
    return re.sub(
        r"^(兑换商品|兌換商品|兑换抵扣券|抵扣券退款)"
        r"\s*[-：:]?\s*",
        "",
        content,
    ).strip()


def _choose_product_order(
    point_record,
    orders,
    shop_mapping,
):
    point_time = _parse_datetime(point_record.get("time"))
    point_subject = _normalize_text(
        _point_subject(point_record)
    )
    candidates = []

    for order in orders:
        if "取消" in str(order.get("status") or ""):
            continue

        venue = _match_venue(
            order.get("shop_mess"),
            shop_mapping,
        )
        if not venue:
            continue

        order_time = _parse_datetime(order.get("pay_time"))
        order_subject = _normalize_text(
            order.get("goods_name")
        )
        subject_match = bool(
            point_subject
            and order_subject
            and (
                point_subject in order_subject
                or order_subject in point_subject
            )
        )
        distance = (
            abs((point_time - order_time).total_seconds())
            if point_time and order_time
            else float("inf")
        )
        candidates.append(
            {
                "venue": venue,
                "record": order,
                "subject_match": subject_match,
                "distance": distance,
                "source": "商品订单",
            }
        )

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: (
            not item["subject_match"],
            item["distance"],
        )
    )
    return candidates[0]


def _choose_coupon_order(
    point_record,
    coupon_orders,
    shop_mapping,
):
    point_time = _parse_datetime(point_record.get("time"))
    point_content = _normalize_text(
        point_record.get("projectContent")
    )
    candidates = []

    for order in coupon_orders:
        venue = _match_venue(
            order.get("verify_shop"),
            shop_mapping,
        )
        if not venue:
            continue

        order_time = _parse_datetime(
            order.get("exchange_time")
        )
        coupon_title = _normalize_text(
            order.get("coupon_title")
        )
        subject_match = bool(
            point_content
            and coupon_title
            and coupon_title in point_content
        )
        distance = (
            abs((point_time - order_time).total_seconds())
            if point_time and order_time
            else float("inf")
        )
        candidates.append(
            {
                "venue": venue,
                "record": order,
                "subject_match": subject_match,
                "distance": distance,
                "source": "抵扣券订单",
            }
        )

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: (
            not item["subject_match"],
            item["distance"],
        )
    )
    return candidates[0]


def _attribute_uncovered_points(
    cloud_client,
    point_records,
    shop_mapping,
):
    exact_shop_names = {
        item["normalized"]
        for item in shop_mapping
    }
    uncovered = [
        record
        for record in point_records
        if _normalize_text(record.get("shop_name"))
        not in exact_shop_names
    ]
    attributed = []
    unresolved = []

    for record in uncovered:
        venue = _match_venue(
            record.get("shop_name"),
            shop_mapping,
        )
        if venue:
            attributed.append(
                {
                    "point": record,
                    "venue": venue,
                    "source": "店名模糊匹配",
                }
            )
        else:
            unresolved.append(record)

    phones = [
        str(record.get("phone") or "").strip()
        for record in unresolved
        if str(record.get("phone") or "").strip()
    ]
    product_orders = (
        cloud_client.query_product_orders_for_phones(phones)
    )
    coupon_orders = (
        cloud_client.query_coupon_orders_for_phones(phones)
    )
    unmatched = []

    for record in unresolved:
        phone = str(record.get("phone") or "").strip()
        if not phone:
            unmatched.append((record, "用户账号为空"))
            continue

        product_match = _choose_product_order(
            record,
            product_orders.get(phone, []),
            shop_mapping,
        )
        coupon_match = _choose_coupon_order(
            record,
            coupon_orders.get(phone, []),
            shop_mapping,
        )
        project_content = str(
            record.get("projectContent") or ""
        )
        if "券" in project_content:
            best_match = coupon_match or product_match
        else:
            best_match = product_match or coupon_match

        if not best_match:
            unmatched.append(
                (record, "没有找到可映射门店的历史订单")
            )
            continue

        attributed.append(
            {
                "point": record,
                "venue": best_match["venue"],
                "source": best_match["source"],
            }
        )

    return uncovered, attributed, unmatched


def main(start_date, end_date, account, password):
    start_dt = _normalize_date(start_date, "start_date")
    end_dt = _normalize_date(end_date, "end_date")
    if start_dt > end_dt:
        raise ValueError("start_date不能晚于end_date")

    start_text = start_dt.strftime("%Y-%m-%d")
    exclusive_end_text = (end_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    detail_start_text = start_dt.strftime("%Y-%m-%d 00:00:00")
    detail_end_text = end_dt.strftime("%Y-%m-%d 23:59:59")

    match_tuples = _get_match_tuples()
    shop_mapping = _build_shop_mapping(match_tuples)

    venue_totals = OrderedDict()
    component_totals = OrderedDict()
    for venue, _ in match_tuples:
        venue_totals[venue] = [Decimal("0"), Decimal("0")]
        component_totals[venue] = {
            "statistics_add": Decimal("0"),
            "statistics_subtract": Decimal("0"),
            "verified_coupon_subtract": Decimal("0"),
        }

    def ensure_venue(venue):
        if venue not in venue_totals:
            venue_totals[venue] = [Decimal("0"), Decimal("0")]
            component_totals[venue] = {
                "statistics_add": Decimal("0"),
                "statistics_subtract": Decimal("0"),
                "verified_coupon_subtract": Decimal("0"),
            }

    failed_shops = []
    session = create_retry_session()
    session.trust_env = False
    session.headers.update({
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    })

    try:
        for venue, shop_names in match_tuples:
            for shop_name in _split_shop_names(shop_names):
                try:
                    add_score, subtract_score = _request_shop_points(
                        session, shop_name, start_text, exclusive_end_text
                    )
                except Exception as error:
                    failed_shops.append((shop_name, str(error)))
                    continue

                ensure_venue(venue)
                venue_totals[venue][0] += add_score
                venue_totals[venue][1] += subtract_score
                component_totals[venue]["statistics_add"] += add_score
                component_totals[venue]["statistics_subtract"] += subtract_score

        if failed_shops:
            failed_names = "、".join(shop_name for shop_name, _ in failed_shops)
            raise RuntimeError(
                f"新系统店铺统计仍有{len(failed_shops)}个店铺请求失败，"
                f"为避免返回不完整数据，本次已停止：{failed_names}"
            )

        cloud_client = CloudBaseClient(session)
        cloud_client.login(account, password)

        verified_coupons = cloud_client.query_verified_coupon_orders(
            detail_start_text, detail_end_text
        )

        matched_coupons = []
        unmatched_coupons = []
        for coupon_order in verified_coupons:
            verify_shop = coupon_order.get("verify_shop")
            venue = _match_venue(verify_shop, shop_mapping)
            if not venue:
                unmatched_coupons.append(coupon_order)
                continue

            amount = abs(_as_decimal(coupon_order.get("point_cost")))
            ensure_venue(venue)
            venue_totals[venue][1] += amount
            component_totals[venue]["verified_coupon_subtract"] += amount
            matched_coupons.append(coupon_order)
    finally:
        session.close()

    result = [
        {
            "场地": venue,
            "新系统积分增加": _plain_number(totals[0]),
            "新系统积分减少": _plain_number(totals[1]),
        }
        for venue, totals in venue_totals.items()
    ]

    print("\n① 店铺统计查询失败数量:", len(failed_shops))
    print("② 已核销抵扣券数量:", len(verified_coupons))
    print("② 已加入场地积分减少数量:", len(matched_coupons))
    print("② 未匹配核销门店数量:", len(unmatched_coupons))
    for coupon_order in unmatched_coupons[:MAX_UNMATCHED_LOGS]:
        print(
            f"- 用户账号={_mask_sensitive(coupon_order.get('user_phone'))}，"
            f"核销时间={coupon_order.get('verify_time')}，"
            f"券名称={coupon_order.get('coupon_title')}，"
            f"核销门店={coupon_order.get('verify_shop')}"
        )
    if len(unmatched_coupons) > MAX_UNMATCHED_LOGS:
        print(f"- 其余{len(unmatched_coupons) - MAX_UNMATCHED_LOGS}条未匹配核销记录已省略")

    component_summary = {
        key: sum((values[key] for values in component_totals.values()), Decimal("0"))
        for key in ("statistics_add", "statistics_subtract", "verified_coupon_subtract")
    }
    print("\n分项汇总:")
    print("① 统计模块积分增加:", _plain_number(component_summary["statistics_add"]))
    print("① 统计模块积分减少:", _plain_number(component_summary["statistics_subtract"]))
    print("② 已核销抵扣券积分减少:", _plain_number(component_summary["verified_coupon_subtract"]))

    print(result)
    return result


if __name__ == "__main__":
    import os, sys
    from datetime import datetime as dt
    account = os.environ.get("NEWSYSTEM_ACCOUNT", "")
    password = os.environ.get("NEWSYSTEM_PASSWORD", "")
    if not account or not password:
        print("请设置环境变量 NEWSYSTEM_ACCOUNT 和 NEWSYSTEM_PASSWORD")
        sys.exit(1)
    if len(sys.argv) >= 3:
        start, end = sys.argv[1], sys.argv[2]
    else:
        d = dt.now().strftime("%Y-%m-%d")
        start, end = d, d
    data = main(start, end, account, password)
    print(f"\n共返回 {len(data)} 条场地数据")
