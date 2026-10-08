# -*- coding: utf-8 -*-
"""会员资产监控的标准化与聚合测试。"""

import os
import sys
from datetime import date, datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.monitoring as monitoring
from core.monitoring import build_monitor_snapshot, get_monitor_snapshot, normalize_monitor_event


def _event(
    event_type,
    amount,
    venue="深圳A店",
    member="13800138000",
    balance_after=100,
    raw=None,
    occurred_at=None,
):
    event = {
        "source": "points" if event_type.startswith("points_") else "balance",
        "external_id": "%s-%s" % (event_type, amount),
        "occurred_at": occurred_at or datetime(2026, 8, 19, 12, 0),
        "venue": venue,
        "member_ref": member,
        "event_type": event_type,
        "amount": amount,
        "balance_after": balance_after,
        "operator": "测试",
    }
    if raw is not None:
        event["raw_json"] = raw
    return event


def test_normalize_monitor_event_generates_stable_external_id():
    raw = {
        "source": "points", "occurred_at": "2026-08-19T12:00:00",
        "venue": "深圳A店", "member_ref": "13800138000",
        "event_type": "points_earn", "amount": "120",
    }
    first = normalize_monitor_event(raw)
    second = normalize_monitor_event(raw)

    assert first["external_id"] == second["external_id"]
    assert first["amount"] == 120.0
    assert first["occurred_at"] == "2026-08-19 12:00:00"


def test_monitor_snapshot_aggregates_and_masks_member():
    events = [
        _event("points_earn", 500),
        _event("points_redeem", -120),
        _event("points_adjust", -2500),
        _event("balance_grant", 3200, raw={
            "store_value_name": "余币", "operation_channel": "管理后台",
        }),
        _event("balance_consume", -800),
    ]
    snapshot = build_monitor_snapshot(events, 1, date(2026, 8, 19), "live")

    assert snapshot["summary"]["points_earned"] == 500
    assert snapshot["summary"]["points_redeemed"] == 2620
    assert snapshot["summary"]["points_net"] == -2120
    assert snapshot["summary"]["balance_net"] == 2400
    assert snapshot["summary"]["alert_count"] == 2
    assert snapshot["activities"][0]["member_ref"] == "13800138000"


def test_monitor_snapshot_filters_category_and_venue():
    events = [
        _event("points_earn", 200, "深圳A店"),
        _event("balance_recharge", 600, "深圳A店"),
        _event("points_earn", 900, "深圳B店"),
    ]
    snapshot = build_monitor_snapshot(
        events, 1, date(2026, 8, 19), "live", category="points", venue="深圳A店"
    )

    assert snapshot["summary"]["transaction_count"] == 1
    assert snapshot["summary"]["points_earned"] == 200
    assert snapshot["summary"]["recharge_amount"] == 0


def test_monitor_snapshot_extracts_assets_and_ranks_top10_per_venue_asset():
    events = [
        _event(
            "balance_consume", -amount, raw={
                "store_value_name": "余币", "operation_channel": "微信小程序",
            },
        )
        for amount in range(1, 13)
    ]
    events.extend(
        _event("balance_recharge", amount, raw={"storeValueName": "弹珠"})
        for amount in (100, 200, 300)
    )
    # 损坏 JSON 不应使整个看板失败。
    events.append(_event("points_earn", 5, raw="{broken"))

    snapshot = build_monitor_snapshot(events, 1, date(2026, 8, 19), "live")

    groups = {}
    for item in snapshot["top_changes"]:
        groups.setdefault((item["venue"], item["asset_name"]), []).append(item)
    assert len(groups[("深圳A店", "余币")]) == 10
    assert [item["abs_amount"] for item in groups[("深圳A店", "余币")]][:2] == [12, 11]
    assert len(groups[("深圳A店", "弹珠")]) == 3
    assert groups[("深圳A店", "余币")][0]["unit"] == "余币"
    assert groups[("深圳A店", "余币")][0]["operation_channel"] == "微信小程序"
    assert snapshot["summary"]["venue_count"] == 1
    assert snapshot["summary"]["asset_count"] == 3
    assert snapshot["summary"]["top10_count"] == 14

    summaries = {item["asset_name"]: item for item in snapshot["asset_summaries"]}
    assert summaries["余币"]["transaction_count"] == 12
    assert summaries["余币"]["decrease_amount"] == 78
    assert summaries["余币"]["top10_count"] == 10


def test_monitor_snapshot_separates_rule_anomalies_from_top10_and_p99_watch():
    events = [
        _event("balance_consume", -5, balance_after=-1, raw={"store_value_name": "负余额资产"}),
        _event("balance_grant", 50, raw={
            "store_value_name": "赠送资产", "operation_channel": "管理后台",
        }),
        _event("balance_adjust", 12, raw={"store_value_name": "复核资产"}),
        _event("balance_refund", 20, raw={"store_value_name": "复核资产"}),
        _event("balance_adjust", 0, raw={"store_value_name": "复核资产"}),
    ]
    events.extend(
        _event(
            "balance_consume", -amount,
            raw={"store_value_name": "统计资产"},
            occurred_at=datetime(2026, 8, 19, 13, amount % 60),
        )
        for amount in list(range(1, 20)) + [1000]
    )

    snapshot = build_monitor_snapshot(events, 1, date(2026, 8, 19), "live")

    levels = snapshot["summary"]["level_counts"]
    assert levels == {"critical": 1, "high": 1, "medium": 2, "watch": 1}
    assert snapshot["summary"]["anomaly_count"] == 5
    assert snapshot["summary"]["top10_count"] > snapshot["summary"]["anomaly_count"]
    assert len(snapshot["anomalies"]) == 5
    assert all(item["reason"] and item["asset_name"] and item["unit"] for item in snapshot["anomalies"])

    watch = next(item for item in snapshot["anomalies"] if item["level"] == "watch")
    assert watch["amount"] == -1000
    assert "严格高于" in watch["reason"]
    flagged_top_change = next(
        item for item in snapshot["top_changes"]
        if item["asset_name"] == "赠送资产"
    )
    assert flagged_top_change["severity"] == "high"
    assert flagged_top_change["alert_title"] == "管理后台赠送待复核"
    assert "管理后台" in flagged_top_change["reason"]
    assert "绝对变动第" in flagged_top_change["rank_reason"]
    small_group = next(
        item for item in snapshot["statistical_analysis"]["groups"]
        if item["asset_name"] == "赠送资产"
    )
    assert small_group["eligible"] is False
    assert "样本不足" in small_group["reason"]


def test_get_monitor_snapshot_marks_unsynced_target_date(monkeypatch):
    monkeypatch.setattr(monitoring, "_load_live_events", lambda start, end: [])
    monkeypatch.setattr(monitoring, "_get_monitor_syncs_for_date", lambda target: {})

    snapshot = get_monitor_snapshot(target_date="2026-08-18")

    assert snapshot["mode"] == "unsynced"
    assert snapshot["mode_label"] == "未同步"
    assert snapshot["target_date"] == "2026-08-18"
    assert snapshot["period"] == {"days": 1, "start": "2026-08-18", "end": "2026-08-18"}
    assert snapshot["summary"]["transaction_count"] == 0
    assert snapshot["top_changes"] == []
    assert snapshot["anomalies"] == []


def test_get_monitor_snapshot_treats_successful_zero_row_date_as_synced(monkeypatch):
    sync = {
        "source": "duojinbao_store_value", "category": "balance",
        "start_date": "2026-08-18", "end_date": "2026-08-18", "status": "success",
        "stores_total": 14, "stores_succeeded": 14, "event_count": 0, "error_count": 0,
        "started_at": "2026-08-19 01:00:00", "finished_at": "2026-08-19 01:01:00",
    }
    monkeypatch.setattr(monitoring, "_load_live_events", lambda start, end: [])
    monkeypatch.setattr(
        monitoring, "_get_monitor_syncs_for_date", lambda target: {"balance": sync}
    )

    snapshot = get_monitor_snapshot(category="balance", target_date=date(2026, 8, 18))

    assert snapshot["mode"] == "live"
    assert snapshot["is_synced"] is True
    assert snapshot["summary"]["stores_total"] == 14
    assert snapshot["summary"]["stores_succeeded"] == 14


def test_get_monitor_snapshot_rejects_invalid_target_date():
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        get_monitor_snapshot(target_date="2026/08/18")


def test_monitor_snapshot_splits_coin_and_points_assets():
    events = [
        _event("balance_consume", -3, raw={"store_value_name": "余币"}),
        _event("balance_recharge", 200, raw={"store_value_name": "弹珠"}),
        _event("balance_adjust", 8, raw={"store_value_name": "娃娃积分"}),
        _event("balance_grant", 20, raw={"store_value_name": "弹珠积分"}),
        _event("balance_adjust", 1, raw={"store_value_name": "新资产"}),
    ]

    coin = build_monitor_snapshot(
        events, 1, date(2026, 8, 19), "live", asset_group="coin"
    )
    points = build_monitor_snapshot(
        events, 1, date(2026, 8, 19), "live", asset_group="points"
    )
    other = build_monitor_snapshot(
        events, 1, date(2026, 8, 19), "live", asset_group="other"
    )

    assert {item["asset_name"] for item in coin["asset_summaries"]} == {"余币", "弹珠"}
    assert coin["summary"]["transaction_count"] == 2
    assert coin["summary"]["coin_transaction_count"] == 2
    assert {item["asset_name"] for item in points["asset_summaries"]} == {
        "娃娃积分", "弹珠积分",
    }
    assert points["summary"]["transaction_count"] == 2
    assert points["summary"]["points_transaction_count"] == 2
    assert next(
        item for item in points["top_changes"] if item["asset_name"] == "弹珠积分"
    )["event_label"] == "积分赠送"
    assert other["summary"]["transaction_count"] == 1
    assert other["asset_summaries"][0]["asset_group"] == "other"


def test_monitor_snapshot_filters_specific_asset_without_changing_legacy_category():
    events = [
        _event("balance_consume", -3, raw={"store_value_name": "余币"}),
        _event("balance_grant", 20, raw={"store_value_name": "弹珠积分"}),
    ]

    legacy = build_monitor_snapshot(
        events, 1, date(2026, 8, 19), "live", category="balance"
    )
    selected = build_monitor_snapshot(
        events, 1, date(2026, 8, 19), "live",
        asset_group="points", asset_name="弹珠积分",
    )

    assert legacy["summary"]["transaction_count"] == 2
    assert selected["summary"]["transaction_count"] == 1
    assert selected["filters"]["asset_name"] == "弹珠积分"
    assert selected["top_changes"][0]["asset_group"] == "points"


def test_monitor_snapshot_filters_coin_grants_by_business_change_type():
    events = [
        _event("balance_grant", 20, raw={"store_value_name": "余币"}),
        _event("balance_grant", 30, raw={"store_value_name": "弹珠积分"}),
        _event("balance_consume", -5, raw={"store_value_name": "余币"}),
    ]

    snapshot = build_monitor_snapshot(
        events, 1, date(2026, 8, 19), "live", change_type="coin:balance_grant"
    )

    assert snapshot["summary"]["transaction_count"] == 1
    assert snapshot["top_changes"][0]["event_label"] == "币赠送"
    assert snapshot["filters"]["change_type"] == "coin:balance_grant"
    assert {item["label"] for item in snapshot["filters"]["change_type_options"]} >= {
        "币赠送", "积分赠送",
    }


def test_get_monitor_snapshot_keeps_synced_mode_when_selected_group_is_empty(monkeypatch):
    sync = {
        "source": "duojinbao_store_value", "category": "balance",
        "start_date": "2026-08-18", "end_date": "2026-08-18", "status": "success",
        "stores_total": 14, "stores_succeeded": 14, "event_count": 0, "error_count": 0,
        "started_at": "2026-08-19 01:00:00", "finished_at": "2026-08-19 01:01:00",
    }
    monkeypatch.setattr(monitoring, "_load_live_events", lambda start, end: [])
    monkeypatch.setattr(
        monitoring, "_get_monitor_syncs_for_date", lambda target: {"balance": sync}
    )

    snapshot = get_monitor_snapshot(
        target_date="2026-08-18", asset_group="points"
    )

    assert snapshot["mode"] == "live"
    assert snapshot["is_synced"] is True
    assert snapshot["summary"]["transaction_count"] == 0


def test_get_monitor_snapshot_rejects_invalid_asset_group():
    with pytest.raises(ValueError, match="asset_group"):
        get_monitor_snapshot(asset_group="money")
