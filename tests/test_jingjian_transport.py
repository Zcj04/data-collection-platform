# -*- coding: utf-8 -*-
"""鲸舰明文 HTTP 必须由部署方显式确认，避免无提示发送凭证。"""

import pytest

from crawlers import jingjian_crawler


def test_http_is_blocked_before_session_creation(monkeypatch):
    monkeypatch.setattr(
        jingjian_crawler,
        "LOGIN_BASE_URL",
        "http://login.jingjian.invalid",
    )
    monkeypatch.setattr(
        jingjian_crawler,
        "DATA_BASE_URL",
        "http://data.jingjian.invalid",
    )
    monkeypatch.delenv("WORKBUDDY_ALLOW_INSECURE_JINGJIAN_HTTP", raising=False)

    def fail_if_called():
        raise AssertionError("安全校验前不应创建网络会话")

    monkeypatch.setattr(jingjian_crawler, "create_retry_session", fail_if_called)
    with pytest.raises(RuntimeError, match="默认已阻止"):
        jingjian_crawler.main("2026-08-01", "2026-08-01", "user", "password")


def test_https_gateway_needs_no_insecure_override(monkeypatch):
    monkeypatch.setattr(
        jingjian_crawler,
        "LOGIN_BASE_URL",
        "https://login-gateway.internal",
    )
    monkeypatch.setattr(
        jingjian_crawler,
        "DATA_BASE_URL",
        "https://data-gateway.internal",
    )
    monkeypatch.delenv("WORKBUDDY_ALLOW_INSECURE_JINGJIAN_HTTP", raising=False)
    jingjian_crawler._validate_transport_policy()


def test_http_requires_explicit_risk_acceptance(monkeypatch, caplog):
    monkeypatch.setattr(
        jingjian_crawler,
        "LOGIN_BASE_URL",
        "http://login.jingjian.invalid",
    )
    monkeypatch.setattr(
        jingjian_crawler,
        "DATA_BASE_URL",
        "http://data.jingjian.invalid",
    )
    monkeypatch.setenv("WORKBUDDY_ALLOW_INSECURE_JINGJIAN_HTTP", "1")
    jingjian_crawler._validate_transport_policy()
    assert "HTTP 兼容模式" in caplog.text
