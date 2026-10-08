# -*- coding: utf-8 -*-
"""多金宝储值接口契约、分页和事件映射测试。"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crawlers.duojinbao_store_value_crawler import (
    StoreValueSchemaError,
    fetch_store_value_records,
    parse_record_page,
    record_to_event,
    split_time_by_five_days,
)


def _record(**overrides):
    row = {
        "accountBenefitId": "benefit-1",
        "accountId": "account-1",
        "createTime": "2026-08-18 12:30:00",
        "actualBenefit": 100.0,
        "accountInitialBalance": 50.0,
        "accountEndBalance": 150.0,
        "orderNo": "order-1",
        "recordType": "充值",
        "storeValueName": "会员储值",
        "operationChannel": "后台",
        "name": "不应入库的姓名",
        "phone": "13800138000",
        "headImg": "https://example.invalid/avatar.png",
    }
    row.update(overrides)
    return row


def _store():
    return {
        "store_id": "store-1",
        "merchant_id": "merchant-1",
        "ad_organization_id": "org-1",
        "store_name": "测试店",
    }


def test_split_time_uses_non_overlapping_five_day_chunks():
    assert split_time_by_five_days("2026-08-01", "2026-08-12") == [
        ("2026-08-01", "2026-08-05"),
        ("2026-08-06", "2026-08-10"),
        ("2026-08-11", "2026-08-12"),
    ]


def test_parse_real_contract_and_rejects_silent_empty_schema():
    items, total, pages = parse_record_page({
        "code": 200,
        "data": {"items": [_record()], "total": 1, "pages": 1},
    })
    assert len(items) == 1
    assert total == 1
    assert pages == 1
    with pytest.raises(StoreValueSchemaError, match="items/total/pages"):
        parse_record_page({"code": 200, "data": {"records": []}})


def test_record_mapping_uses_signed_delta_and_removes_pii_from_raw():
    event = record_to_event(_record(recordType="赠送"), _store(), "标准场地")

    assert event["event_type"] == "balance_grant"
    assert event["amount"] == 100.0
    assert event["balance_after"] == 150.0
    assert event["venue"] == "标准场地"
    assert event["member_ref"] == "account-1"
    raw_text = str(event["raw"])
    assert "13800138000" not in raw_text
    assert "不应入库的姓名" not in raw_text
    assert "headImg" not in raw_text


def test_record_mapping_prefers_user_id_over_account_id():
    event = record_to_event(
        _record(accountId="account-1", userId="member-123"), _store(), "标准场地"
    )

    assert event["member_ref"] == "member-123"


def test_record_mapping_detects_balance_mismatch():
    with pytest.raises(StoreValueSchemaError, match="余额差"):
        record_to_event(
            _record(actualBenefit=90, accountInitialBalance=50, accountEndBalance=150),
            _store(),
            "标准场地",
        )


def test_composite_identity_avoids_duplicate_platform_primary_id():
    first = record_to_event(_record(), _store(), "标准场地")
    second = record_to_event(
        _record(createTime="2026-08-18 12:31:00", orderNo="order-2"),
        _store(),
        "标准场地",
    )
    assert first["external_id"] != second["external_id"]


class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self):
        self.pages = []

    def request(self, method, url, params=None, json=None, timeout=None):
        page = json["pageIndex"]
        self.pages.append(page)
        rows = {
            1: [_record(orderNo="1"), _record(orderNo="2", createTime="2026-08-18 12:31:00")],
            2: [_record(orderNo="3", createTime="2026-08-18 12:32:00")],
        }[page]
        return _FakeResponse({
            "code": 200,
            "data": {"items": rows, "total": 3, "pages": 2},
        })


def test_fetch_paginates_until_verified_total():
    session = _FakeSession()
    rows = fetch_store_value_records(
        session, "store-1", "2026-08-18", "2026-08-18"
    )
    assert session.pages == [1, 2]
    assert len(rows) == 3


class _FivePageSession:
    def __init__(self):
        self.pages = []

    def request(self, method, url, params=None, json=None, timeout=None):
        page = json["pageIndex"]
        self.pages.append(page)
        row = _record(
            orderNo="page-%s" % page,
            createTime="2026-08-18 12:%02d:00" % page,
        )
        return _FakeResponse({
            "code": 200,
            "data": {"items": [row], "total": 5, "pages": 5},
        })


def test_fetch_reads_every_page_declared_by_first_page():
    session = _FivePageSession()

    rows = fetch_store_value_records(
        session, "store-1", "2026-08-18", "2026-08-18"
    )

    assert session.pages == [1, 2, 3, 4, 5]
    assert len(rows) == 5


class _MetadataDriftSession:
    def __init__(self):
        self.pages = []

    def request(self, method, url, params=None, json=None, timeout=None):
        page = json["pageIndex"]
        self.pages.append(page)
        metadata = (
            {"total": 2, "pages": 2}
            if page == 1
            else {"total": 3, "pages": 3}
        )
        row = _record(
            orderNo="drift-%s" % page,
            createTime="2026-08-18 13:%02d:00" % page,
        )
        return _FakeResponse({
            "code": 200,
            "data": {"items": [row], **metadata},
        })


def test_fetch_rejects_total_or_pages_drift_after_first_page():
    session = _MetadataDriftSession()

    with pytest.raises(StoreValueSchemaError, match="分页元数据发生变化"):
        fetch_store_value_records(
            session, "store-1", "2026-08-18", "2026-08-18"
        )

    assert session.pages == [1, 2]


class _OutOfRangeSession:
    def request(self, method, url, params=None, json=None, timeout=None):
        return _FakeResponse({
            "code": 200,
            "data": {
                "items": [_record(createTime="2026-08-19 00:00:00")],
                "total": 1,
                "pages": 1,
            },
        })


def test_fetch_rejects_record_outside_requested_date_range():
    with pytest.raises(StoreValueSchemaError, match="请求日期范围之外"):
        fetch_store_value_records(
            _OutOfRangeSession(), "store-1", "2026-08-18", "2026-08-18"
        )
