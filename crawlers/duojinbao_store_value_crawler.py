# -*- coding: utf-8 -*-
"""多金宝会员储值变更采集。

该模块只负责从平台读取并转换为 ``monitor_events`` 标准事件，不保存账号密码、
不打印会员姓名/手机号，也不导出包含个人信息的 JSON/CSV。
"""

import hashlib
import json
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import requests


BASE_URL = "https://djb.leyaoyao.com"
TIMEOUT = 30
PAGE_SIZE = 100
MAX_PAGES = 1000
SOURCE = "duojinbao_store_value"


class StoreValueCrawlerError(RuntimeError):
    """储值采集的可诊断错误，不包含响应正文或凭证。"""


class StoreValueSchemaError(StoreValueCrawlerError):
    """接口成功返回，但结构或字段不符合已验证契约。"""


def _decimal(value: Any, field: str) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise StoreValueSchemaError("字段 %s 不是有效数字" % field) from exc


def _int_value(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise StoreValueSchemaError("字段 %s 不是有效整数" % field) from exc


def split_time_by_five_days(start_date: str, end_date: str) -> List[Tuple[str, str]]:
    start = datetime.strptime(str(start_date).strip(), "%Y-%m-%d")
    end = datetime.strptime(str(end_date).strip(), "%Y-%m-%d")
    if start > end:
        raise ValueError("start_date 不能晚于 end_date")
    chunks: List[Tuple[str, str]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=4), end)
        chunks.append((cursor.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def create_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": BASE_URL,
        "Referer": BASE_URL + "/pages/smallground_b/index.html",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    })
    return session


def request_json(
    session: requests.Session,
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    json_data: Optional[Dict[str, Any]] = None,
    read_retry_count: int = 0,
) -> Dict[str, Any]:
    """请求 JSON；读取型报表 POST 可显式有限重试，登录不重试。"""
    last_error: Optional[BaseException] = None
    for attempt in range(max(0, read_retry_count) + 1):
        try:
            response = session.request(
                method=method,
                url=BASE_URL + path,
                params=params,
                json=json_data,
                timeout=TIMEOUT,
            )
            if response.status_code in (401, 403):
                raise StoreValueCrawlerError("登录会话已失效")
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise StoreValueCrawlerError("接口未返回 JSON: %s" % path) from exc
            if not isinstance(payload, dict):
                raise StoreValueSchemaError("接口顶层响应不是对象: %s" % path)
            if str(payload.get("code")) != "200":
                raise StoreValueCrawlerError(
                    str(payload.get("message") or "接口请求失败: %s" % path)[:300]
                )
            return payload
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            if attempt >= read_retry_count:
                break
            time.sleep(min(4, 2 ** attempt))
        except requests.HTTPError as exc:
            last_error = exc
            status = exc.response.status_code if exc.response is not None else 0
            if attempt >= read_retry_count or status not in (429, 500, 502, 503, 504):
                break
            time.sleep(min(4, 2 ** attempt))
    raise StoreValueCrawlerError("网络请求失败: %s" % path) from last_error


def login(session: requests.Session, username: str, password: str) -> None:
    if not str(username or "").strip() or not str(password or ""):
        raise StoreValueCrawlerError("账号或密码为空")
    result = request_json(
        session, "POST", "/gw/venue/login",
        json_data={"name": str(username).strip(), "password": str(password)},
    )
    if not isinstance(result.get("data"), dict) or not result["data"].get("user"):
        raise StoreValueSchemaError("登录响应缺少用户信息")


def get_all_stores(session: requests.Session) -> List[Dict[str, Any]]:
    result = request_json(
        session, "GET", "/gw/venue/api/v1/merchant/store/staff/merchant",
        read_retry_count=2,
    )
    merchants = result.get("data") or []
    if isinstance(merchants, dict):
        merchants = [merchants]
    if not isinstance(merchants, list):
        raise StoreValueSchemaError("门店接口 data 不是数组或对象")
    stores: List[Dict[str, Any]] = []
    seen = set()
    for merchant in merchants:
        if not isinstance(merchant, dict):
            continue
        merchant_id = merchant.get("merchantId")
        tenant_orgs = merchant.get("tenantOrgList") or []
        if not isinstance(tenant_orgs, list):
            raise StoreValueSchemaError("tenantOrgList 不是数组")
        for store in tenant_orgs:
            if not isinstance(store, dict):
                continue
            store_id = store.get("id")
            organization_id = store.get("adOrganizationId")
            key = (str(merchant_id or ""), str(store_id or ""))
            if store.get("type") != 2 or not store_id or not organization_id or key in seen:
                continue
            seen.add(key)
            stores.append({
                "store_name": str(store.get("name") or "").strip(),
                "store_id": store_id,
                "merchant_id": merchant_id,
                "ad_organization_id": organization_id,
            })
    if not stores:
        raise StoreValueCrawlerError("登录成功，但没有获取到可访问门店")
    return stores


def switch_store(session: requests.Session, store: Dict[str, Any]) -> None:
    request_json(
        session, "GET", "/gw/venue/api/v1/merchant/store/staff/resources",
        params={
            "storeId": store["store_id"],
            "merchantId": store["merchant_id"],
            "adOrganizationId": store["ad_organization_id"],
        },
        read_retry_count=2,
    )


def parse_record_page(payload: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], int, int]:
    """按 2026-08-19 脱敏实测契约解析 data.items/total/pages。"""
    data = payload.get("data")
    if not isinstance(data, dict):
        raise StoreValueSchemaError("储值接口缺少 data 对象")
    if "items" not in data or "total" not in data or "pages" not in data:
        raise StoreValueSchemaError("储值接口缺少 items/total/pages")
    items = data.get("items")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise StoreValueSchemaError("储值接口 items 不是对象数组")
    total = _int_value(data.get("total"), "total")
    pages = _int_value(data.get("pages"), "pages")
    if total < 0 or pages < 0:
        raise StoreValueSchemaError("total/pages 不能为负数")
    return items, total, pages


def _raw_identity(record: Dict[str, Any], store_id: Any) -> str:
    """平台主 ID 实测可重复，使用已对账的组合字段生成稳定行 ID。"""
    fields = (
        store_id,
        record.get("accountBenefitId"),
        record.get("accountId"),
        record.get("createTime"),
        record.get("actualBenefit"),
        record.get("orderNo"),
        record.get("recordType"),
    )
    stable = json.dumps(fields, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def _validate_record_date(
    record: Dict[str, Any],
    start_date: datetime,
    end_date: datetime,
    page_index: int,
) -> None:
    """确保接口没有返回请求日期范围之外的流水。"""
    raw_value = str(record.get("createTime") or "").strip()
    if not raw_value:
        raise StoreValueSchemaError("第 %s 页储值记录缺少 createTime" % page_index)
    try:
        occurred_at = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StoreValueSchemaError(
            "第 %s 页储值记录 createTime 不是有效日期时间" % page_index
        ) from exc
    if not start_date.date() <= occurred_at.date() <= end_date.date():
        raise StoreValueSchemaError(
            "第 %s 页返回请求日期范围之外的储值记录" % page_index
        )


def fetch_store_value_records(
    session: requests.Session,
    store_id: Any,
    start_date: str,
    end_date: str,
) -> List[Dict[str, Any]]:
    requested_start = datetime.strptime(str(start_date).strip(), "%Y-%m-%d")
    requested_end = datetime.strptime(str(end_date).strip(), "%Y-%m-%d")
    if requested_start > requested_end:
        raise ValueError("start_date 不能晚于 end_date")
    records: List[Dict[str, Any]] = []
    identities = set()
    page_signatures = set()
    page_index = 1
    expected_total: Optional[int] = None
    expected_pages: Optional[int] = None
    while True:
        if page_index > MAX_PAGES:
            raise StoreValueCrawlerError("分页超过安全上限 %s，已停止以避免截断" % MAX_PAGES)
        result = request_json(
            session,
            "POST",
            "/gw/venue/api/v1/report/member/storeValue/record",
            json_data={
                "storeIdList": [store_id],
                "startTime": "%s 00:00:00" % start_date,
                "endTime": "%s 23:59:59" % end_date,
                "pageIndex": page_index,
                "pageSize": PAGE_SIZE,
            },
            read_retry_count=2,
        )
        rows, total, pages = parse_record_page(result)
        if expected_total is None:
            expected_total, expected_pages = total, pages
            if expected_total == 0:
                if rows:
                    raise StoreValueSchemaError("total 为 0 但接口仍返回储值记录")
                return []
            if expected_pages <= 0:
                raise StoreValueSchemaError("total 大于 0 但 pages 不是正数")
            if expected_pages > MAX_PAGES:
                raise StoreValueCrawlerError(
                    "分页超过安全上限 %s，已停止以避免截断" % MAX_PAGES
                )
        elif total != expected_total or pages != expected_pages:
            raise StoreValueSchemaError(
                "分页元数据发生变化，第 %s 页 total/pages=%s/%s，第一页为 %s/%s"
                % (page_index, total, pages, expected_total, expected_pages)
            )
        if not rows:
            raise StoreValueSchemaError("分页未完成但第 %s 页为空" % page_index)
        for row in rows:
            _validate_record_date(row, requested_start, requested_end, page_index)
        page_ids = [_raw_identity(row, store_id) for row in rows]
        signature = hashlib.sha256("|".join(page_ids).encode("ascii")).hexdigest()
        if signature in page_signatures:
            raise StoreValueSchemaError("接口重复返回相同分页，第 %s 页" % page_index)
        page_signatures.add(signature)
        for identity, row in zip(page_ids, rows):
            if identity in identities:
                continue
            identities.add(identity)
            records.append(row)
        if page_index == expected_pages:
            break
        page_index += 1
    if page_index != expected_pages:
        raise StoreValueSchemaError(
            "分页记录数不完整，期望抓取 %s 页，实际抓取 %s 页"
            % (expected_pages, page_index)
        )
    if expected_total is not None and len(records) != expected_total:
        raise StoreValueSchemaError(
            "分页记录数不完整，期望 %s 条，去重后得到 %s 条" % (expected_total, len(records))
        )
    return records


def _event_type(record_type: Any, amount: Decimal) -> str:
    label = "" if record_type is None else str(record_type).strip()
    if "退款" in label or "退还" in label:
        return "balance_refund"
    if "充值" in label:
        return "balance_recharge"
    if "赠送" in label or "赠" in label:
        return "balance_grant"
    if "消费" in label or amount < 0:
        return "balance_consume"
    return "balance_adjust"


def _member_ref(record: Dict[str, Any]) -> str:
    """返回接口提供的会员 ID；优先使用用户级 ID，账户 ID 作为兜底。"""
    return next((
        str(record.get(field) or "").strip()
        for field in ("userId", "merchantUserId", "accountId", "cardNo")
        if str(record.get(field) or "").strip()
    ), "匿名会员")


def record_to_event(record: Dict[str, Any], store: Dict[str, Any], venue: str) -> Dict[str, Any]:
    if not str(record.get("createTime") or "").strip():
        raise StoreValueSchemaError("储值记录缺少 createTime")
    amount = _decimal(record.get("actualBenefit"), "actualBenefit")
    initial_raw = record.get("accountInitialBalance")
    end_raw = record.get("accountEndBalance")
    initial = _decimal(initial_raw, "accountInitialBalance")
    balance_after = _decimal(end_raw, "accountEndBalance")
    if initial_raw not in (None, "") and end_raw not in (None, ""):
        balance_delta = balance_after - initial
        if abs(balance_delta - amount) > Decimal("0.001"):
            raise StoreValueSchemaError("actualBenefit 与期初/期末余额差不一致")
    record_type = record.get("recordType")
    return {
        "source": SOURCE,
        "external_id": _raw_identity(record, store["store_id"]),
        "occurred_at": str(record["createTime"]).strip(),
        "venue": venue or store.get("store_name") or "未知门店",
        "member_ref": _member_ref(record),
        "event_type": _event_type(record_type, amount),
        "amount": float(amount),
        "balance_after": float(balance_after) if end_raw not in (None, "") else None,
        "operator": str(record_type or record.get("operationChannel") or "自动同步"),
        "raw": {
            "account_benefit_id": record.get("accountBenefitId"),
            "order_no": record.get("orderNo"),
            "record_type": record_type,
            "store_value_name": record.get("storeValueName"),
            "operation_channel": record.get("operationChannel"),
            "terminal_name": record.get("terminalName"),
            "asset_equipment_name": record.get("assetEquipmentName"),
            "account_initial_balance": float(initial) if initial_raw not in (None, "") else None,
            "account_end_balance": float(balance_after) if end_raw not in (None, "") else None,
        },
    }


def load_venue_map() -> Dict[str, str]:
    """复用现有多金宝门店映射；映射库不可用时保留平台门店名。"""
    try:
        from crawlers.duojinbao_crawler import SHOP_NAME_ALIASES, get_match_list

        mapping = {
            str(shop_name).strip(): str(venue).strip()
            for venue, shop_name in get_match_list()
            if str(shop_name or "").strip()
        }
        for original, alias in SHOP_NAME_ALIASES.items():
            if alias in mapping:
                mapping[original] = mapping[alias]
        return mapping
    except Exception:
        return {}


def collect_store_value_events(
    start_date: str,
    end_date: str,
    accounts: Sequence[Dict[str, str]],
    progress_callback: Optional[Callable[[str], None]] = None,
    venue_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """采集日期范围内全部可访问门店，按店铺隔离错误并返回标准事件。"""
    chunks = split_time_by_five_days(start_date, end_date)
    valid_accounts = [
        account for account in accounts
        if str(account.get("username") or "").strip() and str(account.get("password") or "")
    ]
    if not valid_accounts:
        raise StoreValueCrawlerError("未配置多金宝账号")
    mapping = load_venue_map() if venue_map is None else dict(venue_map)
    processed_store_keys = set()
    events: List[Dict[str, Any]] = []
    errors: List[str] = []
    stores_total = 0
    stores_succeeded = 0
    account_success = 0

    for account_index, account in enumerate(valid_accounts, 1):
        session = create_session()
        try:
            try:
                login(session, account["username"], account["password"])
                stores = get_all_stores(session)
                account_success += 1
            except Exception as exc:
                errors.append("账号%s登录或读取门店失败: %s" % (account_index, str(exc)))
                continue
            for store in stores:
                store_key = (str(store["merchant_id"]), str(store["store_id"]))
                if store_key in processed_store_keys:
                    continue
                stores_total += 1
                store_name = store.get("store_name") or "未命名门店"
                if progress_callback:
                    progress_callback("正在同步储值: %s" % store_name)
                try:
                    switch_store(session, store)
                    store_records: List[Dict[str, Any]] = []
                    for chunk_start, chunk_end in chunks:
                        store_records.extend(fetch_store_value_records(
                            session, store["store_id"], chunk_start, chunk_end
                        ))
                    venue = mapping.get(str(store_name).strip(), str(store_name).strip())
                    store_events = [record_to_event(row, store, venue) for row in store_records]
                except Exception as exc:
                    errors.append("%s: %s" % (store_name, str(exc)))
                    continue
                events.extend(store_events)
                stores_succeeded += 1
                processed_store_keys.add(store_key)
        finally:
            session.close()

    if account_success == 0:
        raise StoreValueCrawlerError("全部账号均无法登录或读取门店")
    return {
        "events": events,
        "stores_total": stores_total,
        "stores_succeeded": stores_succeeded,
        "errors": errors,
        "event_count": len(events),
    }
